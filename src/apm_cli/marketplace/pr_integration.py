"""Pull request integration for marketplace publish.

Wraps the ``gh`` CLI to open or update pull requests on consumer
repositories after the publisher has pushed update branches.

This module is a library only -- no CLI wiring.  The CLI command
(``apm marketplace publish``) is wired in a later wave.

Design
------
* **No pushing**: ``PrIntegrator`` only reads PR state and opens or
  updates PRs.  Safe-force-push coordination is the caller's
  responsibility.
* **Token redaction**: stderr from ``gh`` subprocesses is redacted
  using the same ``_TOKEN_RE`` pattern as ``publisher.py``.
* **Error isolation**: a failing ``gh`` call returns ``PrState.FAILED``
  rather than raising -- callers can continue with other targets.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .git_stderr import translate_git_stderr
from .publisher import ConsumerTarget, PublishOutcome, PublishPlan, TargetResult

__all__ = [
    "PrState",
    "PrResult",
    "PrIntegrator",
]

# ---------------------------------------------------------------------------
# Token redaction (same approach as publisher.py / ref_resolver.py)
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"https://[^@]*@")


def _redact_token(text: str) -> str:
    """Remove ``https://<anything>@`` token patterns from *text*."""
    return _TOKEN_RE.sub("https://***@", text)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class PrState(str, Enum):
    """Outcome of a PR operation on a single consumer target."""

    OPENED = "opened"  # new PR created
    UPDATED = "updated"  # existing PR for the branch already open
    SKIPPED = "skipped"  # no update needed (non-UPDATED outcome)
    FAILED = "failed"  # gh call failed
    DISABLED = "disabled"  # --no-pr was set for this target


@dataclass(frozen=True)
class PrResult:
    """Result of a PR operation on a single consumer target."""

    target: ConsumerTarget
    state: PrState
    pr_number: int | None  # set when OPENED or UPDATED
    pr_url: str | None  # set when OPENED or UPDATED
    message: str  # human-readable detail


# ---------------------------------------------------------------------------
# PR URL parsing
# ---------------------------------------------------------------------------

_PR_NUMBER_RE = re.compile(r"/pull/(\d+)")


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------


def _extract_short_hash(plan: PublishPlan) -> str:
    """Return the short hash from *plan*, falling back to the branch name.

    The branch name is ``apm/marketplace-update-{name}-{ver}-{hash}``
    so the hash is the last segment after the final ``-``.
    """
    if plan.short_hash:
        return plan.short_hash
    # Derive from branch_name -- it ends with "-{short_hash}"
    parts = plan.branch_name.rsplit("-", 1)
    if len(parts) == 2:
        return parts[1]
    return ""


def _build_title(plan: PublishPlan) -> str:
    """Build the PR title."""
    return (
        f"chore(apm): bump {plan.marketplace_name} "
        f"to {plan.marketplace_version}"
    )


def _build_body(plan: PublishPlan, target: ConsumerTarget) -> str:
    """Build the PR body."""
    short_hash = _extract_short_hash(plan)
    return (
        f"Automated update from `apm marketplace publish`.\n"
        f"\n"
        f"- Marketplace: `{plan.marketplace_name}`\n"
        f"- New version: `{plan.marketplace_version}`\n"
        f"- New ref: `{plan.new_ref}`\n"
        f"- Branch: `{plan.branch_name}`\n"
        f"\n"
        f"This PR updates `dependencies.apm` entries that reference "
        f"`{plan.marketplace_name}` "
        f"in `{target.path_in_repo}`.\n"
        f"\n"
        f"<!-- APM-Publish-Id: {short_hash} -->\n"
    )


# ---------------------------------------------------------------------------
# PrIntegrator service
# ---------------------------------------------------------------------------


class PrIntegrator:
    """Open or update pull requests on consumer repositories.

    Wraps the ``gh`` CLI.  All subprocess calls go through the
    injectable *runner* so tests can fake them without real processes.

    Parameters
    ----------
    runner:
        Callable with the same signature as ``subprocess.run``.
        Defaults to ``subprocess.run``.
    gh_bin:
        Path or name of the ``gh`` binary.  Defaults to ``"gh"``.
    timeout_s:
        Timeout in seconds for each ``gh`` invocation.
    """

    def __init__(
        self,
        *,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
        gh_bin: str = "gh",
        timeout_s: float = 30.0,
    ) -> None:
        self._runner = runner or subprocess.run
        self._gh_bin = gh_bin
        self._timeout_s = timeout_s

    # -- availability check -------------------------------------------------

    def check_available(self) -> tuple[bool, str]:
        """Return ``(True, version)`` if gh is installed and authenticated.

        Returns ``(False, hint)`` otherwise.
        """
        # 1. Check gh is installed
        try:
            result = self._runner(
                [self._gh_bin, "--version"],
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
            )
            if result.returncode != 0:
                return (
                    False,
                    "gh CLI not found on PATH. Install from "
                    "https://cli.github.com/ or pass --no-pr.",
                )
            version = result.stdout.strip()
        except (OSError, FileNotFoundError):
            return (
                False,
                "gh CLI not found on PATH. Install from "
                "https://cli.github.com/ or pass --no-pr.",
            )

        # 2. Check gh is authenticated
        try:
            auth_result = self._runner(
                [self._gh_bin, "auth", "status"],
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
            )
            if auth_result.returncode != 0:
                return (
                    False,
                    "gh CLI is not authenticated. Run "
                    "'gh auth login' or pass --no-pr.",
                )
        except (OSError, FileNotFoundError):
            return (
                False,
                "gh CLI is not authenticated. Run "
                "'gh auth login' or pass --no-pr.",
            )

        return (True, version)

    # -- open or update -----------------------------------------------------

    def open_or_update(
        self,
        plan: PublishPlan,
        target: ConsumerTarget,
        target_result: TargetResult,
        *,
        no_pr: bool = False,
        draft: bool = False,
        dry_run: bool = False,
    ) -> PrResult:
        """Open or update a PR on the consumer repository.

        Parameters
        ----------
        plan:
            The publish plan for this run.
        target:
            The consumer repository target.
        target_result:
            The result of the publish step for this target.
        no_pr:
            If ``True``, skip PR creation entirely.
        draft:
            If ``True``, create the PR as a draft.
        dry_run:
            If ``True``, do not actually create the PR.

        Returns
        -------
        PrResult
            The outcome of the PR operation.
        """
        if no_pr:
            return PrResult(
                target=target,
                state=PrState.DISABLED,
                pr_number=None,
                pr_url=None,
                message="PR creation disabled (--no-pr).",
            )

        if target_result.outcome != PublishOutcome.UPDATED:
            return PrResult(
                target=target,
                state=PrState.SKIPPED,
                pr_number=None,
                pr_url=None,
                message=f"No PR needed: {target_result.outcome.value}",
            )

        try:
            return self._open_or_update_inner(
                plan, target, draft=draft, dry_run=dry_run,
            )
        except subprocess.CalledProcessError as exc:
            stderr = _redact_token(exc.stderr or "")
            translated = translate_git_stderr(
                stderr,
                exit_code=exc.returncode,
                operation="gh pr",
                remote=target.repo,
            )
            return PrResult(
                target=target,
                state=PrState.FAILED,
                pr_number=None,
                pr_url=None,
                message=f"gh failed: {translated.summary} -- {stderr}",
            )
        except subprocess.TimeoutExpired:
            return PrResult(
                target=target,
                state=PrState.FAILED,
                pr_number=None,
                pr_url=None,
                message=f"gh timed out after {self._timeout_s}s.",
            )
        except OSError as exc:
            return PrResult(
                target=target,
                state=PrState.FAILED,
                pr_number=None,
                pr_url=None,
                message=f"OS error running gh: {exc}",
            )

    # -- internal methods ---------------------------------------------------

    def _open_or_update_inner(
        self,
        plan: PublishPlan,
        target: ConsumerTarget,
        *,
        draft: bool = False,
        dry_run: bool = False,
    ) -> PrResult:
        """Core logic for open_or_update, without error handling."""
        # 1. Check for existing PR
        existing = self._find_existing_pr(plan, target)

        title = _build_title(plan)
        body = _build_body(plan, target)

        if existing is not None:
            # Existing PR found
            pr_number = existing["number"]
            pr_url = existing["url"]
            existing_body = existing.get("body", "")

            if body == existing_body:
                return PrResult(
                    target=target,
                    state=PrState.UPDATED,
                    pr_number=pr_number,
                    pr_url=pr_url,
                    message="PR already open, body unchanged.",
                )

            # Update the PR body
            self._update_pr_body(target, pr_number, body)
            return PrResult(
                target=target,
                state=PrState.UPDATED,
                pr_number=pr_number,
                pr_url=pr_url,
                message="PR body updated.",
            )

        # 2. No existing PR -- create
        if dry_run:
            return PrResult(
                target=target,
                state=PrState.OPENED,
                pr_number=None,
                pr_url=None,
                message="[dry-run] Would open PR.",
            )

        pr_url, pr_number = self._create_pr(
            plan, target, title, body, draft=draft,
        )

        return PrResult(
            target=target,
            state=PrState.OPENED,
            pr_number=pr_number,
            pr_url=pr_url,
            message="PR opened.",
        )

    def _find_existing_pr(
        self,
        plan: PublishPlan,
        target: ConsumerTarget,
    ) -> Optional[dict]:
        """Return the first open PR for *plan.branch_name*, or ``None``."""
        result = self._runner(
            [
                self._gh_bin, "pr", "list",
                "--repo", target.repo,
                "--head", plan.branch_name,
                "--state", "open",
                "--json", "number,url,body,headRefOid",
                "--limit", "1",
            ],
            capture_output=True,
            text=True,
            timeout=self._timeout_s,
            check=True,
        )

        try:
            prs = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise OSError(
                f"Failed to parse gh pr list output: {exc}"
            ) from exc

        if not prs:
            return None
        return prs[0]

    def _update_pr_body(
        self,
        target: ConsumerTarget,
        pr_number: int,
        body: str,
    ) -> None:
        """Update the body of an existing PR."""
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".md",
            delete=False,
            encoding="utf-8",
        ) as fh:
            fh.write(body)
            tmp_path = fh.name

        try:
            self._runner(
                [
                    self._gh_bin, "pr", "edit",
                    str(pr_number),
                    "--repo", target.repo,
                    "--body-file", tmp_path,
                ],
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                check=True,
            )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _create_pr(
        self,
        plan: PublishPlan,
        target: ConsumerTarget,
        title: str,
        body: str,
        *,
        draft: bool = False,
    ) -> tuple[str, int]:
        """Create a new PR and return ``(url, number)``."""
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".md",
            delete=False,
            encoding="utf-8",
        ) as fh:
            fh.write(body)
            tmp_path = fh.name

        try:
            cmd = [
                self._gh_bin, "pr", "create",
                "--repo", target.repo,
                "--base", target.branch,
                "--head", plan.branch_name,
                "--title", title,
                "--body-file", tmp_path,
            ]
            if draft:
                cmd.append("--draft")

            result = self._runner(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                check=True,
            )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        # Parse the PR URL from stdout (last non-empty line)
        lines = result.stdout.strip().splitlines()
        pr_url = lines[-1].strip() if lines else ""

        match = _PR_NUMBER_RE.search(pr_url)
        pr_number = int(match.group(1)) if match else 0

        return pr_url, pr_number
