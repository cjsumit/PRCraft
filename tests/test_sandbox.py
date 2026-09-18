import pytest

from app.sandbox import LocalSandbox, PathBoundaryError


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOX_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("GITHUB_TOKEN", "test")
    monkeypatch.setenv("GITHUB_BOT_USERNAME", "test-bot")
    from app.config import get_settings

    get_settings.cache_clear()
    box = LocalSandbox(workspace_id="test-workspace")
    yield box
    box.cleanup()


def test_write_and_read_file(sandbox):
    sandbox.write_file("src/example.py", "print('hello')\n")
    content = sandbox.read_file("src/example.py")
    assert "print('hello')" in content
    assert content.strip().startswith("1")


def test_list_dir(sandbox):
    sandbox.write_file("a.txt", "a")
    sandbox.write_file("sub/b.txt", "b")
    entries = sandbox.list_dir(".")
    assert "a.txt" in entries
    assert "sub/" in entries


def test_path_traversal_is_blocked(sandbox):
    with pytest.raises(PathBoundaryError):
        sandbox.read_file("../../etc/passwd")


def test_absolute_path_escape_is_blocked(sandbox):
    with pytest.raises(PathBoundaryError):
        sandbox.write_file("/etc/passwd", "pwned")


def test_run_command_executes_in_workspace(sandbox):
    sandbox.write_file("marker.txt", "present")
    result = sandbox.run_command("ls")
    assert result.ok
    assert "marker.txt" in result.stdout


def test_reset_clears_leftover_files_from_a_prior_run(sandbox):
    sandbox.write_file("repo/stale_clone_marker.txt", "leftover from a previous failed run")
    assert sandbox.workspace_path.exists()

    sandbox.reset()

    assert sandbox.workspace_path.exists()
    assert list(sandbox.workspace_path.iterdir()) == []
    sandbox.write_file("repo/new_file.txt", "fresh content")
    assert "leftover" not in sandbox.read_file("repo/new_file.txt")


def test_reset_removes_readonly_files(sandbox):
    """Read-only Git files should not prevent workspace cleanup."""
    import os
    import stat

    target = sandbox.workspace_path / "repo" / ".git" / "objects" / "aa"
    target.mkdir(parents=True)
    locked_file = target / "deadbeef"
    locked_file.write_text("git object data")
    os.chmod(locked_file, stat.S_IREAD)

    sandbox.reset()

    assert list(sandbox.workspace_path.iterdir()) == []


def test_append_file_creates_new_file(sandbox):
    sandbox.append_file("notes.txt", "first line\n")
    assert "first line" in sandbox.read_file("notes.txt")


def test_append_file_extends_existing_file(sandbox):
    sandbox.write_file("big.txt", "chunk one\n")
    sandbox.append_file("big.txt", "chunk two\n")
    sandbox.append_file("big.txt", "chunk three\n")
    content = sandbox.read_file("big.txt")
    assert "chunk one" in content
    assert "chunk two" in content
    assert "chunk three" in content


def test_append_file_blocked_by_path_traversal(sandbox):
    with pytest.raises(PathBoundaryError):
        sandbox.append_file("../../etc/passwd", "pwned")
