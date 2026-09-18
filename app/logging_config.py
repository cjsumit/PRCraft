"""Structured JSON logging setup for the CLI and its supporting modules."""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from typing import Any

import orjson


class JsonFormatter(logging.Formatter):
    """Serializes log records as single-line JSON for easy ingestion."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        reserved = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys())
        for key, value in record.__dict__.items():
            if key not in reserved and key not in payload:
                payload[key] = value

        return orjson.dumps(payload).decode()


def configure_logging(level: str = "INFO") -> None:
    """Configure the root logger once, at process startup."""
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(level.upper())
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())

    for noisy in ("httpx", "urllib3", "docker"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
