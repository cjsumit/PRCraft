"""Environment-backed application settings."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Literal["development", "staging", "production"] = Field(
        default="development", alias="APP_ENV"
    )
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    anthropic_api_key: str = Field(..., alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(
        default="claude-sonnet-5", alias="ANTHROPIC_MODEL"
    )
    agent_max_iterations: int = Field(default=25, alias="AGENT_MAX_ITERATIONS")
    agent_max_output_tokens: int = Field(default=8192, alias="AGENT_MAX_OUTPUT_TOKENS")

    github_token: str = Field(..., alias="GITHUB_TOKEN")
    github_bot_username: str = Field(..., alias="GITHUB_BOT_USERNAME")
    github_bot_email: str = Field(
        default="bot@example.com", alias="GITHUB_BOT_EMAIL"
    )
    github_api_base_url: str = Field(
        default="https://api.github.com", alias="GITHUB_API_BASE_URL"
    )

    sandbox_backend: Literal["local", "docker"] = Field(
        default="docker", alias="SANDBOX_BACKEND"
    )
    sandbox_docker_image: str = Field(
        default="python:3.11-slim", alias="SANDBOX_DOCKER_IMAGE"
    )
    sandbox_workspace_root: str = Field(
        default="/tmp/ghbot-workspaces", alias="SANDBOX_WORKSPACE_ROOT"
    )
    sandbox_command_timeout_seconds: int = Field(
        default=120, alias="SANDBOX_COMMAND_TIMEOUT_SECONDS"
    )
    sandbox_max_file_bytes: int = Field(
        default=2_000_000, alias="SANDBOX_MAX_FILE_BYTES"
    )
    sandbox_keep_workspace_on_failure: bool = Field(
        default=True, alias="SANDBOX_KEEP_WORKSPACE_ON_FAILURE"
    )


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings singleton (loaded once per process)."""
    return Settings()  # type: ignore[call-arg]
