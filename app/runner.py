"""Synchronous issue-to-pull-request pipeline."""
from __future__ import annotations

from dataclasses import dataclass

from app.agent import CodingAgent
from app.github_service import GitHubService, GitHubServiceError
from app.logging_config import get_logger
from app.models import GitHubIssueRef, JobStatus
from app.notifier import ConsoleReporter
from app.sandbox import SandboxError, build_sandbox

logger = get_logger(__name__)


@dataclass
class RunResult:
    status: JobStatus
    pr_url: str | None = None
    pr_number: int | None = None
    reason: str | None = None


def run_issue_pipeline(ref: GitHubIssueRef, reporter: ConsoleReporter | None = None) -> RunResult:
    """
    Fetch the issue, resolve repo access, run the coding agent in a sandbox,
    and open a PR. Returns a RunResult describing the outcome; never raises
    for expected failure modes (GitHub/sandbox errors) -- those are reported
    and returned as a FAILED result. Unexpected exceptions propagate.
    """
    reporter = reporter or ConsoleReporter()
    status = JobStatus.STARTED
    sandbox = None

    try:
        reporter.started(ref.issue_number, ref.full_name)

        status = JobStatus.FETCHING_ISSUE
        gh = GitHubService()
        issue = gh.fetch_issue(ref)
        logger.info("issue_fetched", extra={"issue": ref.full_name, "title": issue.title})
        reporter.progress(f"Fetched issue: \"{issue.title}\"")

        status = JobStatus.PREPARING_REPO
        reporter.progress(f"Preparing repository {ref.full_name}...")
        access = gh.resolve_repo_access(ref)
        if access.is_fork:
            reporter.progress(f"No push access -- using fork {access.push_owner}/{access.push_repo}")

        sandbox = build_sandbox(workspace_id=f"issue-{ref.owner}-{ref.repo}-{ref.issue_number}")
        sandbox.reset()
        repo_dir = sandbox.workspace_path / "repo"
        gh.clone_repo(access, repo_dir)
        gh.configure_identity(repo_dir)

        branch_name = f"fix/issue-{ref.issue_number}"
        gh.create_branch(repo_dir, branch_name, base_branch=issue.default_branch)
        reporter.progress(f"Created branch {branch_name}")

        status = JobStatus.RUNNING_AGENT
        reporter.progress("Analyzing the issue and working on a fix (this can take a while)...")

        agent_sandbox = build_sandbox(workspace_id=sandbox.workspace_id)
        agent_sandbox.workspace_path = repo_dir  # scope the agent's tools to the repo root
        agent = CodingAgent(sandbox=agent_sandbox)
        result = agent.solve_issue(issue)

        if not result.success:
            reason = result.error or "the agent could not resolve the issue."
            reporter.failure(reason)
            return RunResult(status=JobStatus.FAILED, reason=reason)

        status = JobStatus.OPENING_PR
        committed = gh.commit_all(repo_dir, message=f"Fix #{ref.issue_number}: {issue.title}")
        if not committed:
            reason = "the agent reported success but made no file changes."
            reporter.failure(reason)
            return RunResult(status=JobStatus.FAILED, reason=reason)

        gh.push_branch(repo_dir, branch_name)
        reporter.progress("Pushed branch, opening pull request...")

        pr_title = f"Fix #{ref.issue_number}: {issue.title}"
        pr = gh.open_pull_request(
            ref=ref,
            access=access,
            branch_name=branch_name,
            title=pr_title,
            body=result.summary,
            base_branch=issue.default_branch,
        )

        reporter.success(pr.pr_url, result.summary)
        return RunResult(status=JobStatus.SUCCEEDED, pr_url=pr.pr_url, pr_number=pr.pr_number)

    except (GitHubServiceError, SandboxError) as exc:
        logger.error("pipeline_failed", extra={"stage": status.value, "error": str(exc)})
        reporter.failure(str(exc))
        return RunResult(status=JobStatus.FAILED, reason=str(exc))

    finally:
        if sandbox is not None:
            from app.config import get_settings

            settings = get_settings()
            keep = settings.sandbox_keep_workspace_on_failure and status != JobStatus.SUCCEEDED
            if not keep:
                sandbox.cleanup()
            else:
                reporter.warn(f"Workspace kept for inspection at: {sandbox.workspace_path}")
