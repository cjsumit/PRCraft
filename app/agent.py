"""Issue resolution loop and tool definitions."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import anthropic

from app.config import get_settings
from app.logging_config import get_logger
from app.models import AgentResult, IssueDetails
from app.sandbox import BaseSandbox, PathBoundaryError, SandboxError

logger = get_logger(__name__)

SYSTEM_PROMPT = """You are an autonomous senior software engineer fixing a GitHub issue \
inside an isolated sandbox that already contains a git clone of the target repository, \
checked out on a fresh branch.

Follow this protocol strictly:
1. ANALYZE: Read the issue title/body/comments/labels. Explore the repository structure \
with list_dir and read_file to identify the file(s) most likely responsible for the bug \
or feature request. Do not guess blindly -- inspect real code before editing it.
2. REPRODUCE: Form a concrete hypothesis about the root cause. Where practical, write or \
identify a minimal test (using the project's existing test framework/conventions) that \
currently fails and demonstrates the issue.
3. FIX: Apply the smallest, most targeted code edit(s) that resolve the root cause. Avoid \
unrelated refactors or formatting-only changes.
4. VERIFY: Run the project's test suite (or the most relevant subset) via run_command. \
Confirm your new/updated test passes and that you have not broken other tests. If tests \
fail, iterate: re-read the failing output, adjust your fix, and re-run.
5. FINISH: Once verification passes (or you have made a best-effort fix and documented any \
remaining caveats), call the `finish_task` tool with a concise PR summary and the list of \
files you changed. Do not call finish_task before you have actually attempted to run \
whatever test/verification command is appropriate for this project.

Rules:
- Only use the provided tools to interact with the repository; you have no other means of \
reading or writing files.
- All paths given to tools are relative to the repository root.
- Keep edits minimal and focused on resolving the issue.
- When writing a large file, don't put all of its content into a single write_file call. \
Write an initial chunk (e.g. the first third) with write_file, then add the rest with one or \
more append_file calls. This keeps each individual response short enough to avoid being cut \
off by the output length limit.
- If, after genuine effort, the issue cannot be resolved (e.g. it requires information or \
access you don't have), call finish_task with success=false and a clear explanation.
- Never fabricate test results -- only report what run_command actually returned.
"""

TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_dir",
        "description": "List files and directories at a given path relative to the repo root.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path to list. Use '.' for the repository root.",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read a file's contents (optionally a line range) relative to the repo root. "
            "Returns content with line numbers."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative file path to read."},
                "start_line": {
                    "type": "integer",
                    "description": "1-indexed first line to read (optional).",
                },
                "end_line": {
                    "type": "integer",
                    "description": "1-indexed last line to read, inclusive (optional).",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Create or overwrite a file at the given relative path with the given full "
            "content. Always writes the complete file content, not a diff. For large files, "
            "prefer writing an initial chunk with write_file and adding the rest with one or "
            "more append_file calls, to keep each individual response within the output "
            "length limit -- a single write_file call with a very large 'content' value risks "
            "being cut off mid-generation before the argument finishes, which fails the call."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative file path to write."},
                "content": {"type": "string", "description": "Full new content of the file."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "append_file",
        "description": (
            "Append content to the end of an existing file (creating it if it doesn't exist "
            "yet). Use this together with write_file to build up a large file across several "
            "smaller calls instead of one large write_file call, so each response stays well "
            "under the output length limit."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative file path to append to."},
                "content": {"type": "string", "description": "Content to append to the file."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "run_command",
        "description": (
            "Run a shell command (e.g. a test suite, linter, or build step) inside the "
            "repository's sandbox working directory. Returns exit code, stdout, and stderr."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "The shell command to execute."},
                "timeout": {
                    "type": "integer",
                    "description": "Optional timeout override in seconds.",
                },
            },
            "required": ["cmd"],
        },
    },
    {
        "name": "finish_task",
        "description": (
            "Call this exactly once, when you are done attempting the fix, to report the "
            "final outcome."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "success": {
                    "type": "boolean",
                    "description": "Whether the issue was successfully resolved and verified.",
                },
                "summary": {
                    "type": "string",
                    "description": "Concise PR-ready summary of the change and why it fixes the issue.",
                },
                "files_changed": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Relative paths of files you created or modified.",
                },
                "test_output": {
                    "type": "string",
                    "description": "Relevant excerpt of the final test/verification output.",
                },
                "error": {
                    "type": "string",
                    "description": "If success=false, a clear explanation of what went wrong.",
                },
            },
            "required": ["success", "summary"],
        },
    },
]


@dataclass
class AgentRunLog:
    """Lightweight trace of the agent run, useful for debugging/observability."""

    turns: list[dict[str, Any]] = field(default_factory=list)

    def add(self, role: str, content: Any) -> None:
        self.turns.append({"role": role, "content": content})


class MissingToolArgument(Exception):
    """Raised when a tool_use block is missing a required argument -- typically because the
    model's response was cut off mid-generation by the output token limit."""

    def __init__(self, tool_name: str, missing_key: str) -> None:
        self.tool_name = tool_name
        self.missing_key = missing_key
        super().__init__(f"{tool_name} call missing required argument '{missing_key}'")


class CodingAgent:
    def __init__(self, sandbox: BaseSandbox, client: anthropic.Anthropic | None = None):
        self.settings = get_settings()
        self.sandbox = sandbox
        self.client = client or anthropic.Anthropic(api_key=self.settings.anthropic_api_key)
        self.run_log = AgentRunLog()

    def solve_issue(self, issue: IssueDetails) -> AgentResult:
        user_prompt = self._build_initial_prompt(issue)
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_prompt}]
        self.run_log.add("user", user_prompt)

        for turn in range(1, self.settings.agent_max_iterations + 1):
            logger.info("agent_turn_start", extra={"turn": turn})
            try:
                response = self.client.messages.create(
                    model=self.settings.anthropic_model,
                    max_tokens=self.settings.agent_max_output_tokens,
                    system=SYSTEM_PROMPT,
                    tools=TOOLS,
                    messages=messages,
                )
            except anthropic.APIError as exc:
                logger.error("anthropic_api_error", extra={"error": str(exc)})
                return AgentResult(
                    success=False,
                    summary="Agent run aborted due to an Anthropic API error.",
                    error=str(exc),
                )

            assistant_content = response.content
            messages.append({"role": "assistant", "content": assistant_content})
            self.run_log.add("assistant", [self._serialize_block(b) for b in assistant_content])

            truncated = response.stop_reason == "max_tokens"
            if truncated:
                logger.warning("agent_response_truncated", extra={"turn": turn})

            tool_use_blocks = [b for b in assistant_content if b.type == "tool_use"]

            finish_call = next((b for b in tool_use_blocks if b.name == "finish_task"), None)
            if finish_call is not None:
                return self._build_result_from_finish(finish_call.input)

            if not tool_use_blocks:
                if truncated:
                    messages.append(
                        {
                            "role": "user",
                            "content": self._truncation_notice(),
                        }
                    )
                    continue
                if response.stop_reason == "end_turn":
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Please continue working the issue using the available tools, "
                                "and call finish_task when you are done."
                            ),
                        }
                    )
                    continue
                break

            tool_results = [self._execute_tool(block) for block in tool_use_blocks]
            if truncated:
                tool_results.append({"type": "text", "text": self._truncation_notice()})
            messages.append({"role": "user", "content": tool_results})
            for result in tool_results:
                self.run_log.add("tool_result", result)

        logger.warning("agent_max_iterations_reached")
        return AgentResult(
            success=False,
            summary="Agent did not converge on a fix within the allotted iterations.",
            error="max_iterations_reached",
        )

    def _build_initial_prompt(self, issue: IssueDetails) -> str:
        comments_block = (
            "\n\n".join(f"Comment: {c}" for c in issue.comments) if issue.comments else "(none)"
        )
        return f"""Repository: {issue.ref.full_name}
Issue #{issue.ref.issue_number}: {issue.title}
Labels: {", ".join(issue.labels) or "(none)"}

Issue description:
{issue.body or "(no description provided)"}

Existing comments:
{comments_block}

Begin by exploring the repository structure to understand its layout before making changes.
"""

    def _execute_tool(self, block: Any) -> dict[str, Any]:
        name = block.name
        tool_input = block.input or {}
        logger.info("agent_tool_call", extra={"tool": name, "input": tool_input})

        try:
            output = self._dispatch_tool(name, tool_input)
            is_error = False
        except MissingToolArgument as exc:
            output = (
                f"Tool error: the '{name}' call was missing the required argument "
                f"'{exc.missing_key}'. This usually happens when a response is cut off by "
                f"the output length limit before the tool call finished generating -- most "
                f"often while trying to write a very large file in a single write_file call. "
                f"Retry with a smaller amount of content: write an initial chunk with "
                f"write_file, then add the rest using one or more append_file calls."
            )
            is_error = True
        except (SandboxError, PathBoundaryError) as exc:
            output = f"Tool error: {exc}"
            is_error = True
        except Exception as exc:  # noqa: BLE001
            output = f"Unexpected tool error: {exc}"
            is_error = True
            logger.exception("agent_tool_unexpected_error", extra={"tool": name})

        return {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": str(output)[:12000],
            "is_error": is_error,
        }

    @staticmethod
    def _truncation_notice() -> str:
        return (
            "[SYSTEM NOTICE] Your previous response was cut off because it reached the "
            "maximum output length before finishing. If you were in the middle of a "
            "write_file call, its 'content' argument likely never completed and the write "
            "did not happen. Please retry with a shorter response: for large files, write an "
            "initial chunk with write_file and add the remainder using one or more "
            "append_file calls, so each individual response stays well under the limit."
        )

    def _dispatch_tool(self, name: str, tool_input: dict[str, Any]) -> str:
        if name == "list_dir":
            entries = self.sandbox.list_dir(tool_input.get("path", "."))
            return "\n".join(entries) if entries else "(empty directory)"

        if name == "read_file":
            path = self._require(tool_input, "path", name)
            return self.sandbox.read_file(
                path,
                start_line=tool_input.get("start_line"),
                end_line=tool_input.get("end_line"),
            )

        if name == "write_file":
            path = self._require(tool_input, "path", name)
            content = self._require(tool_input, "content", name)
            self.sandbox.write_file(path, content)
            return f"Wrote {len(content)} bytes to {path}"

        if name == "append_file":
            path = self._require(tool_input, "path", name)
            content = self._require(tool_input, "content", name)
            self.sandbox.append_file(path, content)
            return f"Appended {len(content)} bytes to {path}"

        if name == "run_command":
            cmd = self._require(tool_input, "cmd", name)
            result = self.sandbox.run_command(cmd, timeout=tool_input.get("timeout"))
            return result.combined_output()

        raise SandboxError(f"Unknown tool: {name}")

    @staticmethod
    def _require(tool_input: dict[str, Any], key: str, tool_name: str) -> Any:
        if key not in tool_input:
            raise MissingToolArgument(tool_name, key)
        return tool_input[key]

    @staticmethod
    def _serialize_block(block: Any) -> dict[str, Any]:
        if block.type == "text":
            return {"type": "text", "text": block.text}
        if block.type == "tool_use":
            return {"type": "tool_use", "name": block.name, "input": block.input}
        return {"type": block.type}

    @staticmethod
    def _build_result_from_finish(payload: dict[str, Any]) -> AgentResult:
        return AgentResult(
            success=bool(payload.get("success", False)),
            summary=payload.get("summary", ""),
            files_changed=payload.get("files_changed", []) or [],
            test_output=payload.get("test_output"),
            error=payload.get("error"),
        )
