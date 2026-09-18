"""Shared data models used across the CLI, agent, and GitHub layers."""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class GitHubIssueRef(BaseModel):
    owner: str
    repo: str
    issue_number: int

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}/issues/{self.issue_number}"


class IssueDetails(BaseModel):
    ref: GitHubIssueRef
    title: str
    body: str
    labels: list[str] = Field(default_factory=list)
    comments: list[str] = Field(default_factory=list)
    default_branch: str


class RepoAccess(BaseModel):
    """Resolved strategy for how the bot will obtain write access to a repo."""

    push_owner: str
    push_repo: str
    is_fork: bool
    clone_url: str
    upstream_owner: str
    upstream_repo: str


class AgentResult(BaseModel):
    success: bool
    summary: str
    files_changed: list[str] = Field(default_factory=list)
    test_output: str | None = None
    error: str | None = None


class PullRequestResult(BaseModel):
    pr_url: str
    pr_number: int
    branch: str


class JobStatus(str, Enum):
    STARTED = "started"
    FETCHING_ISSUE = "fetching_issue"
    PREPARING_REPO = "preparing_repo"
    RUNNING_AGENT = "running_agent"
    OPENING_PR = "opening_pr"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
