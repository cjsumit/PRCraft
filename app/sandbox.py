"""Local and Docker-backed execution sandboxes."""
from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
import tempfile
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings
from app.logging_config import get_logger

logger = get_logger(__name__)


class SandboxError(RuntimeError):
    """Raised for sandbox setup/execution failures."""


class PathBoundaryError(SandboxError):
    """Raised when a tool call attempts to escape the sandbox workspace."""


def _clear_readonly_and_retry(func, path, exc_info) -> None:
    """Clear a read-only bit and retry a failed removal."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:  # noqa: BLE001
        pass


def force_remove_tree(path: Path) -> None:
    """Delete a directory tree and move any undeletable remainder aside."""
    if not path.exists():
        return

    shutil.rmtree(path, onerror=_clear_readonly_and_retry)

    if path.exists():
        stale_path = path.parent / f"{path.name}.stale-{uuid.uuid4().hex[:8]}"
        try:
            path.rename(stale_path)
            logger.warning(
                "workspace_partial_delete_moved_aside",
                extra={"path": str(path), "moved_to": str(stale_path)},
            )
        except OSError as exc:
            logger.warning(
                "workspace_cleanup_failed", extra={"path": str(path), "error": str(exc)}
            )


@dataclass
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def combined_output(self, max_chars: int = 8000) -> str:
        text = f"$ exit_code={self.exit_code}\nSTDOUT:\n{self.stdout}\nSTDERR:\n{self.stderr}"
        if self.timed_out:
            text += "\n[Process timed out and was killed]"
        return text[:max_chars]


class BaseSandbox(ABC):
    """Common interface implemented by every sandbox backend."""

    def __init__(self, workspace_id: str | None = None):
        self.settings = get_settings()
        self.workspace_id = workspace_id or f"job-{uuid.uuid4().hex[:12]}"
        self.workspace_root = Path(self.settings.sandbox_workspace_root)
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.workspace_path = self.workspace_root / self.workspace_id
        self.workspace_path.mkdir(parents=True, exist_ok=True)

    def _resolve_safe_path(self, relative_path: str) -> Path:
        """Resolve `relative_path` against the workspace and reject escapes."""
        candidate = (self.workspace_path / relative_path).resolve()
        workspace_resolved = self.workspace_path.resolve()
        try:
            candidate.relative_to(workspace_resolved)
        except ValueError as exc:
            raise PathBoundaryError(
                f"Path '{relative_path}' escapes the sandbox workspace boundary."
            ) from exc
        return candidate

    @abstractmethod
    def list_dir(self, path: str = ".") -> list[str]: ...

    @abstractmethod
    def read_file(
        self, path: str, start_line: int | None = None, end_line: int | None = None
    ) -> str: ...

    @abstractmethod
    def write_file(self, path: str, content: str) -> None: ...

    @abstractmethod
    def append_file(self, path: str, content: str) -> None: ...

    @abstractmethod
    def run_command(self, cmd: str, timeout: int | None = None) -> CommandResult: ...

    def cleanup(self) -> None:
        """Remove the workspace directory. Safe to call multiple times."""
        force_remove_tree(self.workspace_path)

    def reset(self) -> None:
        """
        Empty out the workspace directory (but keep it present), discarding
        anything left over from a previous run at the same workspace id --
        e.g. a partial clone kept around after a prior failed attempt.
        """
        force_remove_tree(self.workspace_path)
        self.workspace_path.mkdir(parents=True, exist_ok=True)

    def __enter__(self) -> "BaseSandbox":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.cleanup()


class LocalSandbox(BaseSandbox):
    """
    Runs commands as host subprocesses, confined (by convention + validation,
    NOT a hard kernel boundary) to `workspace_path`. Suitable for local
    development or trusted CI runners. Use DockerSandbox for untrusted code.
    """

    def list_dir(self, path: str = ".") -> list[str]:
        target = self._resolve_safe_path(path)
        if not target.exists():
            raise SandboxError(f"Path does not exist: {path}")
        if not target.is_dir():
            raise SandboxError(f"Path is not a directory: {path}")
        entries = []
        for item in sorted(target.iterdir()):
            if item.name in {".git", "__pycache__", "node_modules", ".venv"}:
                continue
            suffix = "/" if item.is_dir() else ""
            entries.append(f"{item.name}{suffix}")
        return entries

    def read_file(
        self, path: str, start_line: int | None = None, end_line: int | None = None
    ) -> str:
        target = self._resolve_safe_path(path)
        if not target.exists():
            raise SandboxError(f"File does not exist: {path}")
        if target.stat().st_size > self.settings.sandbox_max_file_bytes:
            raise SandboxError(f"File too large to read directly: {path}")

        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        if start_line is None and end_line is None:
            selected = lines
            offset = 1
        else:
            start = max((start_line or 1) - 1, 0)
            end = end_line or len(lines)
            selected = lines[start:end]
            offset = start + 1

        numbered = [f"{i + offset:>5} | {line}" for i, line in enumerate(selected)]
        return "\n".join(numbered)

    def write_file(self, path: str, content: str) -> None:
        target = self._resolve_safe_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if len(content.encode()) > self.settings.sandbox_max_file_bytes:
            raise SandboxError(f"Refusing to write file larger than configured max: {path}")
        target.write_text(content, encoding="utf-8")
        logger.info("sandbox_write_file", extra={"path": str(target), "bytes": len(content)})

    def append_file(self, path: str, content: str) -> None:
        target = self._resolve_safe_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        existing_size = target.stat().st_size if target.exists() else 0
        if existing_size + len(content.encode()) > self.settings.sandbox_max_file_bytes:
            raise SandboxError(f"Refusing to grow file past the configured max size: {path}")
        with target.open("a", encoding="utf-8") as f:
            f.write(content)
        logger.info(
            "sandbox_append_file", extra={"path": str(target), "appended_bytes": len(content)}
        )

    def run_command(self, cmd: str, timeout: int | None = None) -> CommandResult:
        timeout = timeout or self.settings.sandbox_command_timeout_seconds
        logger.info("sandbox_run_command", extra={"cmd": cmd, "cwd": str(self.workspace_path)})
        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                cwd=self.workspace_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            return CommandResult(exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                exit_code=-1,
                stdout=exc.stdout or "",
                stderr=(exc.stderr or "") + "\n[timeout]",
                timed_out=True,
            )


class DockerSandbox(BaseSandbox):
    """
    Runs each command inside a fresh, network-disabled Docker container with
    the workspace directory bind-mounted at /workspace. This gives real
    kernel-level isolation from the host for untrusted code.

    Network is disabled by default for `run_command` (e.g. running tests);
    package installation, if needed, should happen in the base image build
    rather than at agent runtime, to avoid granting the sandbox internet
    access.
    """

    def __init__(self, workspace_id: str | None = None, network_enabled: bool = False):
        super().__init__(workspace_id)
        self._ensure_docker_available()
        self.image = self.settings.sandbox_docker_image
        self.network_enabled = network_enabled

    @staticmethod
    def _ensure_docker_available() -> None:
        if shutil.which("docker") is None:
            raise SandboxError(
                "Docker CLI not found on PATH. Install Docker or set "
                "SANDBOX_BACKEND=local."
            )

    def list_dir(self, path: str = ".") -> list[str]:
        return LocalSandbox.list_dir(self, path)  # type: ignore[arg-type]

    def read_file(
        self, path: str, start_line: int | None = None, end_line: int | None = None
    ) -> str:
        return LocalSandbox.read_file(self, path, start_line, end_line)  # type: ignore[arg-type]

    def write_file(self, path: str, content: str) -> None:
        LocalSandbox.write_file(self, path, content)  # type: ignore[arg-type]

    def append_file(self, path: str, content: str) -> None:
        LocalSandbox.append_file(self, path, content)  # type: ignore[arg-type]

    def run_command(self, cmd: str, timeout: int | None = None) -> CommandResult:
        timeout = timeout or self.settings.sandbox_command_timeout_seconds
        docker_cmd = [
            "docker", "run", "--rm",
            "--workdir", "/workspace",
            "--volume", f"{self.workspace_path}:/workspace",
            "--cpus", "1.5",
            "--memory", "1g",
            "--pids-limit", "256",
            "--security-opt", "no-new-privileges",
        ]
        if not self.network_enabled:
            docker_cmd += ["--network", "none"]
        docker_cmd += [self.image, "bash", "-lc", cmd]

        logger.info("sandbox_docker_run", extra={"cmd": cmd, "image": self.image})
        try:
            proc = subprocess.run(
                docker_cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            return CommandResult(exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                exit_code=-1,
                stdout=exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
                stderr="[timeout] container killed",
                timed_out=True,
            )


def build_sandbox(workspace_id: str | None = None) -> BaseSandbox:
    """Factory that returns the configured sandbox backend."""
    settings = get_settings()
    if settings.sandbox_backend == "docker":
        try:
            return DockerSandbox(workspace_id=workspace_id)
        except SandboxError as exc:
            logger.warning(
                "docker_sandbox_unavailable_falling_back_to_local", extra={"error": str(exc)}
            )
            return LocalSandbox(workspace_id=workspace_id)
    return LocalSandbox(workspace_id=workspace_id)
