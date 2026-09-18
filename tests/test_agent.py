"""Tests for tool dispatch and incomplete tool calls."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.agent import CodingAgent, MissingToolArgument
from app.sandbox import LocalSandbox


@dataclass
class FakeToolUseBlock:
    """Minimal stand-in for the SDK's tool_use content block."""

    id: str
    name: str
    input: dict


@pytest.fixture()
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOX_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("GITHUB_TOKEN", "test")
    monkeypatch.setenv("GITHUB_BOT_USERNAME", "test-bot")
    from app.config import get_settings

    get_settings.cache_clear()
    sandbox = LocalSandbox(workspace_id="agent-test-workspace")

    class _NoClient:
        pass

    return CodingAgent(sandbox=sandbox, client=_NoClient())  # type: ignore[arg-type]


def test_write_file_missing_content_reports_helpful_error(agent):
    """
    Reproduces the real-world failure: the model's write_file call was cut
    off before the 'content' field was generated, so `.input` only has
    'path'. Previously this raised a raw KeyError; it should now be caught
    and turned into a tool_result that explains what happened and how to
    recover, instead of crashing / confusing the model into looping forever.
    """
    block = FakeToolUseBlock(
        id="toolu_1", name="write_file", input={"path": "src/big_file.py"}
    )
    result = agent._execute_tool(block)

    assert result["is_error"] is True
    assert "missing the required argument 'content'" in result["content"]
    assert "append_file" in result["content"]
    assert not (agent.sandbox.workspace_path / "src" / "big_file.py").exists()


def test_write_file_with_content_succeeds(agent):
    block = FakeToolUseBlock(
        id="toolu_2", name="write_file", input={"path": "ok.py", "content": "print(1)\n"}
    )
    result = agent._execute_tool(block)

    assert result["is_error"] is False
    assert "Wrote" in result["content"]


def test_append_file_missing_content_reports_helpful_error(agent):
    block = FakeToolUseBlock(id="toolu_3", name="append_file", input={"path": "ok.py"})
    result = agent._execute_tool(block)

    assert result["is_error"] is True
    assert "missing the required argument 'content'" in result["content"]


def test_require_raises_missing_tool_argument():
    with pytest.raises(MissingToolArgument):
        CodingAgent._require({"path": "x"}, "content", "write_file")
