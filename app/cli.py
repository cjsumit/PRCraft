"""Command-line entrypoint."""
from __future__ import annotations

import argparse
import sys

from app.config import get_settings
from app.github_service import extract_issue_refs
from app.logging_config import configure_logging, get_logger
from app.models import JobStatus
from app.notifier import ConsoleReporter
from app.runner import run_issue_pipeline

logger = get_logger(__name__)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ghbot",
        description="Paste a GitHub issue link; get back a Pull Request that fixes it.",
    )
    parser.add_argument(
        "issue",
        nargs="?",
        help="A GitHub issue URL, e.g. https://github.com/owner/repo/issues/42 "
        "(or any text containing one). If omitted, you'll be prompted.",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="After finishing, prompt again for another issue instead of exiting.",
    )
    return parser.parse_args(argv)


def _prompt_for_issue(reporter: ConsoleReporter) -> str | None:
    try:
        text = input("Paste a GitHub issue link (or 'q' to quit): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if text.lower() in {"q", "quit", "exit"}:
        return None
    return text or None


def _run_one(raw_input_text: str, reporter: ConsoleReporter) -> int:
    refs = extract_issue_refs(raw_input_text)
    if not refs:
        reporter.warn(
            "No GitHub issue URL found. Expected something like "
            "https://github.com/owner/repo/issues/123"
        )
        return 1

    if len(refs) > 1:
        reporter.warn(f"Found {len(refs)} issue links; only the first one will be processed.")

    ref = refs[0]
    result = run_issue_pipeline(ref, reporter=reporter)
    return 0 if result.status == JobStatus.SUCCEEDED else 1


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    settings = get_settings()
    configure_logging(settings.log_level)

    reporter = ConsoleReporter()

    if args.issue:
        exit_code = _run_one(args.issue, reporter)
        if not args.loop:
            return exit_code

    while True:
        text = _prompt_for_issue(reporter)
        if text is None:
            return 0
        _run_one(text, reporter)


if __name__ == "__main__":
    raise SystemExit(main())
