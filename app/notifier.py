"""Timestamped console progress reporting."""
from __future__ import annotations

import sys
from datetime import datetime

_RESET = "\033[0m"
_COLORS = {
    "info": "\033[36m",
    "success": "\033[32m",
    "error": "\033[31m",
    "warn": "\033[33m",
}


def _supports_color() -> bool:
    return sys.stdout.isatty()


class ConsoleReporter:
    """Prints progress updates for a single issue-fixing run to stdout."""

    def __init__(self, use_color: bool | None = None) -> None:
        self.use_color = self.__class__._supports_color() if use_color is None else use_color

    @staticmethod
    def _supports_color() -> bool:
        return _supports_color()

    def _print(self, level: str, message: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {message}"
        if self.use_color:
            color = _COLORS.get(level, "")
            line = f"{color}{line}{_RESET}"
        print(line, flush=True)

    def started(self, issue_number: int, repo_full_name: str) -> None:
        self._print("info", f"⏳ Processing issue #{issue_number} in {repo_full_name}...")

    def progress(self, message: str) -> None:
        self._print("info", f"🔧 {message}")

    def success(self, pr_url: str, summary: str) -> None:
        self._print("success", f"✅ PR Created: {pr_url}")
        print(f"\nSummary:\n{summary}\n", flush=True)

    def failure(self, error_reason: str) -> None:
        self._print("error", f"❌ Failed: {error_reason}")

    def warn(self, message: str) -> None:
        self._print("warn", f"⚠️  {message}")
