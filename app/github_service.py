"""GitHub issue, repository, and pull request operations."""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from github import Github, GithubException
from github.Repository import Repository

from app.config import get_settings
from app.logging_config import get_logger
from app.models import GitHubIssueRef, IssueDetails, PullRequestResult, RepoAccess

logger = get_logger(__name__)

ISSUE_URL_PATTERN = re.compile(
    r"https?://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)"
    r"/issues/(?P<number>\d+)",
    re.IGNORECASE,
)


class GitHubServiceError(RuntimeError):
    """Raised for any unrecoverable failure in the GitHub integration layer."""


def extract_issue_refs(text: str) -> list[GitHubIssueRef]:
    """Extract all GitHub issue URLs referenced in a chunk of free text."""
    refs: list[GitHubIssueRef] = []
    for match in ISSUE_URL_PATTERN.finditer(text):
        refs.append(
            GitHubIssueRef(
                owner=match.group("owner"),
                repo=match.group("repo"),
                issue_number=int(match.group("number")),
            )
        )
    return refs


class GitHubService:
    def __init__(self, token: str | None = None) -> None:
        self.settings = get_settings()
        self.token = token or self.settings.github_token
        self.client = Github(base_url=self._rest_base_url(), login_or_token=self.token)
        self._bot_login: str | None = None

    def _rest_base_url(self) -> str:
        # PyGithub expects the API base (not the GraphQL/UI host).
        return self.settings.github_api_base_url

    @property
    def bot_login(self) -> str:
        if self._bot_login is None:
            self._bot_login = self.client.get_user().login
        return self._bot_login

    def fetch_issue(self, ref: GitHubIssueRef) -> IssueDetails:
        try:
            repo = self.client.get_repo(ref.full_name)
            issue = repo.get_issue(ref.issue_number)
        except GithubException as exc:
            raise GitHubServiceError(
                f"Failed to fetch issue {ref.full_name}#{ref.issue_number}: {exc.data}"
            ) from exc

        comments = [c.body for c in issue.get_comments() if c.body]
        return IssueDetails(
            ref=ref,
            title=issue.title or "",
            body=issue.body or "",
            labels=[label.name for label in issue.labels],
            comments=comments,
            default_branch=repo.default_branch,
        )

    def resolve_repo_access(self, ref: GitHubIssueRef) -> RepoAccess:
        """
        Decide whether the bot can push branches directly to the target repo,
        or needs to fork it first (typical for public/external repos where
        the bot account has no write access).
        """
        try:
            repo = self.client.get_repo(ref.full_name)
        except GithubException as exc:
            raise GitHubServiceError(f"Repository not found: {ref.full_name}") from exc

        can_push = self._has_push_access(repo)
        if can_push:
            return RepoAccess(
                push_owner=ref.owner,
                push_repo=ref.repo,
                is_fork=False,
                clone_url=self._authenticated_url(repo.clone_url),
                upstream_owner=ref.owner,
                upstream_repo=ref.repo,
            )

        fork = self._get_or_create_fork(repo)
        return RepoAccess(
            push_owner=self.bot_login,
            push_repo=fork.name,
            is_fork=True,
            clone_url=self._authenticated_url(fork.clone_url),
            upstream_owner=ref.owner,
            upstream_repo=ref.repo,
        )

    def _has_push_access(self, repo: Repository) -> bool:
        try:
            permissions = repo.permissions
            return bool(permissions and permissions.push)
        except GithubException:
            return False

    def _get_or_create_fork(self, repo: Repository) -> Repository:
        bot_user = self.client.get_user()
        existing_full_name = f"{self.bot_login}/{repo.name}"
        try:
            return self.client.get_repo(existing_full_name)
        except GithubException:
            pass

        logger.info("creating_fork", extra={"source": repo.full_name})
        fork = bot_user.create_fork(repo)
        self._wait_for_fork_ready(fork)
        return fork

    @staticmethod
    def _wait_for_fork_ready(fork: Repository, attempts: int = 10, delay_s: float = 2.0) -> None:
        """GitHub forks are created asynchronously; poll briefly until clonable."""
        for _ in range(attempts):
            try:
                fork.update()
                if fork.id:
                    return
            except GithubException:
                pass
            time.sleep(delay_s)

    def _authenticated_url(self, https_clone_url: str) -> str:
        return https_clone_url.replace("https://", f"https://x-access-token:{self.token}@")

    def clone_repo(self, access: RepoAccess, dest_dir: Path) -> None:
        cmd = ["git", "clone", "--depth", "50", access.clone_url, str(dest_dir)]
        self._run_git(cmd, cwd=dest_dir.parent)

        if access.is_fork:
            upstream_url = f"https://github.com/{access.upstream_owner}/{access.upstream_repo}.git"
            self._run_git(["git", "remote", "add", "upstream", upstream_url], cwd=dest_dir)

    def create_branch(self, repo_dir: Path, branch_name: str, base_branch: str) -> None:
        self._run_git(["git", "fetch", "origin", base_branch], cwd=repo_dir)
        self._run_git(
            ["git", "checkout", "-b", branch_name, f"origin/{base_branch}"], cwd=repo_dir
        )

    def configure_identity(self, repo_dir: Path) -> None:
        self._run_git(
            ["git", "config", "user.name", self.settings.github_bot_username], cwd=repo_dir
        )
        self._run_git(
            ["git", "config", "user.email", self.settings.github_bot_email], cwd=repo_dir
        )

    def commit_all(self, repo_dir: Path, message: str) -> bool:
        """Stage and commit all changes. Returns False if there was nothing to commit."""
        self._run_git(["git", "add", "-A"], cwd=repo_dir)
        status = self._run_git(["git", "status", "--porcelain"], cwd=repo_dir)
        if not status.stdout.strip():
            return False
        self._run_git(["git", "commit", "-m", message], cwd=repo_dir)
        return True

    def push_branch(self, repo_dir: Path, branch_name: str) -> None:
        self._run_git(["git", "push", "-u", "origin", branch_name, "--force"], cwd=repo_dir)

    def open_pull_request(
        self,
        ref: GitHubIssueRef,
        access: RepoAccess,
        branch_name: str,
        title: str,
        body: str,
        base_branch: str,
    ) -> PullRequestResult:
        upstream_repo = self.client.get_repo(f"{access.upstream_owner}/{access.upstream_repo}")
        head = f"{access.push_owner}:{branch_name}" if access.is_fork else branch_name

        full_body = f"{body}\n\nFixes #{ref.issue_number}"
        try:
            pr = upstream_repo.create_pull(
                title=title,
                body=full_body,
                head=head,
                base=base_branch,
                maintainer_can_modify=True,
            )
        except GithubException as exc:
            raise GitHubServiceError(f"Failed to open pull request: {exc.data}") from exc

        return PullRequestResult(pr_url=pr.html_url, pr_number=pr.number, branch=branch_name)

    def _run_git(self, cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
        logger.info("git_command", extra={"cmd": " ".join(self._redact(c) for c in cmd)})
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        if proc.returncode != 0:
            raise GitHubServiceError(
                f"git command failed ({' '.join(self._redact(c) for c in cmd)}): {proc.stderr.strip()}"
            )
        return proc

    def _redact(self, value: str) -> str:
        return value.replace(self.token, "***") if self.token in value else value
