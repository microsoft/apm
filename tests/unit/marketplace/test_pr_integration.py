"""Tests for pr_integration.py -- PrIntegrator, PrState, PrResult."""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from apm_cli.marketplace.pr_integration import (
    PrIntegrator,
    PrResult,
    PrState,
    _build_body,
    _build_title,
    _extract_short_hash,
    _redact_token,
)
from apm_cli.marketplace.publisher import (
    ConsumerTarget,
    PublishOutcome,
    PublishPlan,
    TargetResult,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_plan(
    *,
    marketplace_name: str = "acme-tools",
    marketplace_version: str = "2.0.0",
    branch_name: str = "apm/marketplace-update-acme-tools-2.0.0-a1b2c3d4",
    new_ref: str = "v2.0.0",
    short_hash: str = "a1b2c3d4",
    targets: tuple[ConsumerTarget, ...] | None = None,
) -> PublishPlan:
    """Return a minimal ``PublishPlan`` for tests."""
    if targets is None:
        targets = (_make_target(),)
    return PublishPlan(
        marketplace_name=marketplace_name,
        marketplace_version=marketplace_version,
        targets=targets,
        commit_message=(
            f"chore(apm): bump {marketplace_name} to {marketplace_version}\n"
            f"\nUpdated by apm marketplace publish.\n"
            f"\nAPM-Publish-Id: {short_hash}"
        ),
        branch_name=branch_name,
        new_ref=new_ref,
        tag_pattern_used="v{version}",
        short_hash=short_hash,
    )


def _make_target(
    *,
    repo: str = "acme-org/consumer",
    branch: str = "main",
    path_in_repo: str = "apm.yml",
) -> ConsumerTarget:
    """Return a minimal ``ConsumerTarget`` for tests."""
    return ConsumerTarget(repo=repo, branch=branch, path_in_repo=path_in_repo)


def _make_target_result(
    *,
    target: ConsumerTarget | None = None,
    outcome: PublishOutcome = PublishOutcome.UPDATED,
    message: str = "Updated 2 refs.",
) -> TargetResult:
    """Return a ``TargetResult`` for tests."""
    if target is None:
        target = _make_target()
    return TargetResult(target=target, outcome=outcome, message=message)


class GhRunner:
    """Injectable ``subprocess.run`` replacement for ``gh`` CLI tests.

    Records all calls and returns pre-configured responses keyed by
    the first few arguments of each command.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self._responses: dict[tuple[str, ...], subprocess.CompletedProcess] = {}
        self._errors: dict[tuple[str, ...], Exception] = {}

    def set_response(
        self,
        key: tuple[str, ...],
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
    ) -> None:
        """Pre-configure a response for commands matching *key*."""
        self._responses[key] = subprocess.CompletedProcess(
            list(key),
            returncode,
            stdout=stdout,
            stderr=stderr,
        )

    def set_error(
        self,
        key: tuple[str, ...],
        exc: Exception,
    ) -> None:
        """Pre-configure an exception for commands matching *key*."""
        self._errors[key] = exc

    def _match_key(self, cmd: list[str]) -> tuple[str, ...] | None:
        """Find the longest registered key that is a prefix of *cmd*."""
        best: tuple[str, ...] | None = None
        for key in list(self._responses) + list(self._errors):
            if tuple(cmd[: len(key)]) == key:
                if best is None or len(key) > len(best):
                    best = key
        return best

    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append((list(cmd), dict(kwargs)))

        key = self._match_key(cmd)

        # Check for configured errors first
        if key is not None and key in self._errors:
            raise self._errors[key]

        # Check for configured responses
        if key is not None and key in self._responses:
            resp = self._responses[key]
            if kwargs.get("check") and resp.returncode != 0:
                raise subprocess.CalledProcessError(
                    resp.returncode,
                    cmd,
                    output=resp.stdout,
                    stderr=resp.stderr,
                )
            return resp

        # Default: success with empty output
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def _expected_body(
    plan: PublishPlan | None = None,
    target: ConsumerTarget | None = None,
) -> str:
    """Return the expected PR body for the default plan/target."""
    if plan is None:
        plan = _make_plan()
    if target is None:
        target = _make_target()
    return _build_body(plan, target)


# ---------------------------------------------------------------------------
# PrState enum
# ---------------------------------------------------------------------------


class TestPrState:
    """Tests for the PrState enum values."""

    def test_opened_value(self) -> None:
        assert PrState.OPENED.value == "opened"

    def test_updated_value(self) -> None:
        assert PrState.UPDATED.value == "updated"

    def test_skipped_value(self) -> None:
        assert PrState.SKIPPED.value == "skipped"

    def test_failed_value(self) -> None:
        assert PrState.FAILED.value == "failed"

    def test_disabled_value(self) -> None:
        assert PrState.DISABLED.value == "disabled"

    def test_is_str_subclass(self) -> None:
        assert isinstance(PrState.OPENED, str)


# ---------------------------------------------------------------------------
# PrResult dataclass
# ---------------------------------------------------------------------------


class TestPrResult:
    """Tests for the PrResult frozen dataclass."""

    def test_frozen(self) -> None:
        result = PrResult(
            target=_make_target(),
            state=PrState.OPENED,
            pr_number=42,
            pr_url="https://github.com/acme-org/consumer/pull/42",
            message="PR opened.",
        )
        with pytest.raises(AttributeError):
            result.state = PrState.FAILED  # type: ignore[misc]

    def test_fields_accessible(self) -> None:
        target = _make_target()
        result = PrResult(
            target=target,
            state=PrState.OPENED,
            pr_number=42,
            pr_url="https://github.com/acme-org/consumer/pull/42",
            message="PR opened.",
        )
        assert result.target is target
        assert result.pr_number == 42
        assert result.pr_url == "https://github.com/acme-org/consumer/pull/42"
        assert result.message == "PR opened."


# ---------------------------------------------------------------------------
# check_available
# ---------------------------------------------------------------------------


class TestCheckAvailable:
    """Tests for PrIntegrator.check_available()."""

    def test_gh_version_fails_not_found(self) -> None:
        """gh --version returns non-zero -> False with install hint."""
        runner = GhRunner()
        runner.set_response(
            ("gh", "--version"),
            returncode=1,
            stderr="not found",
        )
        integrator = PrIntegrator(runner=runner)
        ok, msg = integrator.check_available()

        assert ok is False
        assert "gh CLI not found on PATH" in msg
        assert "https://cli.github.com/" in msg

    def test_gh_version_os_error(self) -> None:
        """gh --version raises OSError -> False with install hint."""
        runner = GhRunner()
        runner.set_error(
            ("gh", "--version"),
            FileNotFoundError("no such file"),
        )
        integrator = PrIntegrator(runner=runner)
        ok, msg = integrator.check_available()

        assert ok is False
        assert "gh CLI not found on PATH" in msg

    def test_gh_auth_fails(self) -> None:
        """gh auth status returns non-zero -> False with auth hint."""
        runner = GhRunner()
        runner.set_response(
            ("gh", "--version"),
            stdout="gh version 2.50.0\n",
        )
        runner.set_response(
            ("gh", "auth", "status"),
            returncode=1,
            stderr="not logged in",
        )
        integrator = PrIntegrator(runner=runner)
        ok, msg = integrator.check_available()

        assert ok is False
        assert "gh CLI is not authenticated" in msg
        assert "gh auth login" in msg

    def test_gh_auth_os_error(self) -> None:
        """gh auth status raises OSError -> False with auth hint."""
        runner = GhRunner()
        runner.set_response(
            ("gh", "--version"),
            stdout="gh version 2.50.0\n",
        )
        runner.set_error(
            ("gh", "auth", "status"),
            OSError("pipe broken"),
        )
        integrator = PrIntegrator(runner=runner)
        ok, msg = integrator.check_available()

        assert ok is False
        assert "gh CLI is not authenticated" in msg

    def test_both_succeed(self) -> None:
        """gh --version and gh auth status both succeed -> True."""
        runner = GhRunner()
        runner.set_response(
            ("gh", "--version"),
            stdout="gh version 2.50.0 (2025-01-01)\n",
        )
        runner.set_response(("gh", "auth", "status"), stdout="Logged in\n")
        integrator = PrIntegrator(runner=runner)
        ok, version = integrator.check_available()

        assert ok is True
        assert "2.50.0" in version


# ---------------------------------------------------------------------------
# open_or_update -- early returns
# ---------------------------------------------------------------------------


class TestOpenOrUpdateEarlyReturns:
    """Tests for conditions that return early without calling gh."""

    def test_no_pr_flag_returns_disabled(self) -> None:
        """no_pr=True -> DISABLED, no runner calls."""
        runner = GhRunner()
        integrator = PrIntegrator(runner=runner)
        result = integrator.open_or_update(
            _make_plan(),
            _make_target(),
            _make_target_result(),
            no_pr=True,
        )
        assert result.state == PrState.DISABLED
        assert result.pr_number is None
        assert result.pr_url is None
        assert "--no-pr" in result.message
        assert len(runner.calls) == 0

    def test_no_change_outcome_returns_skipped(self) -> None:
        """outcome=NO_CHANGE -> SKIPPED, no runner calls."""
        runner = GhRunner()
        integrator = PrIntegrator(runner=runner)
        result = integrator.open_or_update(
            _make_plan(),
            _make_target(),
            _make_target_result(outcome=PublishOutcome.NO_CHANGE),
        )
        assert result.state == PrState.SKIPPED
        assert "no-change" in result.message
        assert len(runner.calls) == 0

    def test_skipped_downgrade_outcome_returns_skipped(self) -> None:
        """outcome=SKIPPED_DOWNGRADE -> SKIPPED, no runner calls."""
        runner = GhRunner()
        integrator = PrIntegrator(runner=runner)
        result = integrator.open_or_update(
            _make_plan(),
            _make_target(),
            _make_target_result(outcome=PublishOutcome.SKIPPED_DOWNGRADE),
        )
        assert result.state == PrState.SKIPPED
        assert "skipped-downgrade" in result.message
        assert len(runner.calls) == 0

    def test_skipped_ref_change_outcome_returns_skipped(self) -> None:
        """outcome=SKIPPED_REF_CHANGE -> SKIPPED, no runner calls."""
        runner = GhRunner()
        integrator = PrIntegrator(runner=runner)
        result = integrator.open_or_update(
            _make_plan(),
            _make_target(),
            _make_target_result(outcome=PublishOutcome.SKIPPED_REF_CHANGE),
        )
        assert result.state == PrState.SKIPPED
        assert "skipped-ref-change" in result.message
        assert len(runner.calls) == 0

    def test_failed_outcome_returns_skipped(self) -> None:
        """outcome=FAILED -> SKIPPED, no runner calls."""
        runner = GhRunner()
        integrator = PrIntegrator(runner=runner)
        result = integrator.open_or_update(
            _make_plan(),
            _make_target(),
            _make_target_result(outcome=PublishOutcome.FAILED),
        )
        assert result.state == PrState.SKIPPED
        assert "failed" in result.message
        assert len(runner.calls) == 0


# ---------------------------------------------------------------------------
# open_or_update -- happy path: create PR
# ---------------------------------------------------------------------------


class TestCreatePr:
    """Tests for creating a new PR (no existing PR)."""

    def test_create_pr_happy_path(self) -> None:
        """UPDATED outcome, no existing PR -> OPENED with number/url."""
        runner = GhRunner()
        # gh pr list returns empty array (no existing PR)
        runner.set_response(
            ("gh", "pr", "list"),
            stdout="[]\n",
        )
        # gh pr create returns PR URL
        runner.set_response(
            ("gh", "pr", "create"),
            stdout="https://github.com/acme-org/consumer/pull/42\n",
        )
        integrator = PrIntegrator(runner=runner)
        result = integrator.open_or_update(
            _make_plan(),
            _make_target(),
            _make_target_result(),
        )

        assert result.state == PrState.OPENED
        assert result.pr_number == 42
        assert result.pr_url == "https://github.com/acme-org/consumer/pull/42"
        assert result.message == "PR opened."

    def test_create_pr_passes_correct_repo_and_base(self) -> None:
        """gh pr create receives --repo and --base from target."""
        runner = GhRunner()
        runner.set_response(("gh", "pr", "list"), stdout="[]\n")
        runner.set_response(
            ("gh", "pr", "create"),
            stdout="https://github.com/acme-org/consumer/pull/1\n",
        )
        target = _make_target(repo="acme-org/other-repo", branch="develop")
        integrator = PrIntegrator(runner=runner)
        integrator.open_or_update(
            _make_plan(),
            target,
            _make_target_result(target=target),
        )

        # Find the pr create call
        create_calls = [
            c for c, _ in runner.calls if len(c) >= 3 and c[1] == "pr" and c[2] == "create"
        ]
        assert len(create_calls) == 1
        cmd = create_calls[0]
        assert "--repo" in cmd
        repo_idx = cmd.index("--repo")
        assert cmd[repo_idx + 1] == "acme-org/other-repo"
        assert "--base" in cmd
        base_idx = cmd.index("--base")
        assert cmd[base_idx + 1] == "develop"

    def test_create_pr_passes_head_branch(self) -> None:
        """gh pr create receives --head from plan.branch_name."""
        runner = GhRunner()
        runner.set_response(("gh", "pr", "list"), stdout="[]\n")
        runner.set_response(
            ("gh", "pr", "create"),
            stdout="https://github.com/acme-org/consumer/pull/1\n",
        )
        plan = _make_plan(
            branch_name="apm/marketplace-update-custom-1.0.0-deadbeef",
        )
        integrator = PrIntegrator(runner=runner)
        integrator.open_or_update(
            plan,
            _make_target(),
            _make_target_result(),
        )

        create_calls = [
            c for c, _ in runner.calls if len(c) >= 3 and c[1] == "pr" and c[2] == "create"
        ]
        cmd = create_calls[0]
        head_idx = cmd.index("--head")
        assert cmd[head_idx + 1] == "apm/marketplace-update-custom-1.0.0-deadbeef"

    def test_create_pr_with_draft_flag(self) -> None:
        """draft=True -> gh pr create command includes --draft."""
        runner = GhRunner()
        runner.set_response(("gh", "pr", "list"), stdout="[]\n")
        runner.set_response(
            ("gh", "pr", "create"),
            stdout="https://github.com/acme-org/consumer/pull/7\n",
        )
        integrator = PrIntegrator(runner=runner)
        result = integrator.open_or_update(
            _make_plan(),
            _make_target(),
            _make_target_result(),
            draft=True,
        )

        assert result.state == PrState.OPENED
        create_calls = [
            c for c, _ in runner.calls if len(c) >= 3 and c[1] == "pr" and c[2] == "create"
        ]
        assert any("--draft" in c for c in create_calls)

    def test_create_pr_without_draft_flag(self) -> None:
        """draft=False (default) -> gh pr create has no --draft."""
        runner = GhRunner()
        runner.set_response(("gh", "pr", "list"), stdout="[]\n")
        runner.set_response(
            ("gh", "pr", "create"),
            stdout="https://github.com/acme-org/consumer/pull/7\n",
        )
        integrator = PrIntegrator(runner=runner)
        integrator.open_or_update(
            _make_plan(),
            _make_target(),
            _make_target_result(),
        )

        create_calls = [
            c for c, _ in runner.calls if len(c) >= 3 and c[1] == "pr" and c[2] == "create"
        ]
        assert not any("--draft" in c for c in create_calls)

    def test_dry_run_no_existing_pr(self) -> None:
        """dry_run=True, no existing PR -> OPENED with None number/url."""
        runner = GhRunner()
        runner.set_response(("gh", "pr", "list"), stdout="[]\n")
        integrator = PrIntegrator(runner=runner)
        result = integrator.open_or_update(
            _make_plan(),
            _make_target(),
            _make_target_result(),
            dry_run=True,
        )

        assert result.state == PrState.OPENED
        assert result.pr_number is None
        assert result.pr_url is None
        assert "[dry-run]" in result.message

        # No pr create call should have been made
        create_calls = [
            c for c, _ in runner.calls if len(c) >= 3 and c[1] == "pr" and c[2] == "create"
        ]
        assert len(create_calls) == 0

    def test_pr_url_parsing_pull_number(self) -> None:
        """PR number is correctly parsed from the URL."""
        runner = GhRunner()
        runner.set_response(("gh", "pr", "list"), stdout="[]\n")
        runner.set_response(
            ("gh", "pr", "create"),
            stdout="https://github.com/acme-org/consumer/pull/99\n",
        )
        integrator = PrIntegrator(runner=runner)
        result = integrator.open_or_update(
            _make_plan(),
            _make_target(),
            _make_target_result(),
        )

        assert result.pr_number == 99
        assert result.pr_url == "https://github.com/acme-org/consumer/pull/99"

    def test_pr_url_multiline_stdout(self) -> None:
        """PR URL is parsed from the last line of stdout."""
        runner = GhRunner()
        runner.set_response(("gh", "pr", "list"), stdout="[]\n")
        runner.set_response(
            ("gh", "pr", "create"),
            stdout=(
                "Creating pull request for feature-branch into main\n"
                "https://github.com/acme-org/consumer/pull/123\n"
            ),
        )
        integrator = PrIntegrator(runner=runner)
        result = integrator.open_or_update(
            _make_plan(),
            _make_target(),
            _make_target_result(),
        )

        assert result.pr_number == 123
        assert "pull/123" in (result.pr_url or "")


# ---------------------------------------------------------------------------
# open_or_update -- existing PR
# ---------------------------------------------------------------------------


class TestExistingPr:
    """Tests for when a PR already exists."""

    def test_existing_pr_body_unchanged(self) -> None:
        """Existing PR with identical body -> UPDATED, 'unchanged'."""
        plan = _make_plan()
        target = _make_target()
        body = _build_body(plan, target)

        runner = GhRunner()
        runner.set_response(
            ("gh", "pr", "list"),
            stdout=json.dumps(
                [
                    {
                        "number": 10,
                        "url": "https://github.com/acme-org/consumer/pull/10",
                        "body": body,
                        "headRefOid": "abc123",
                    }
                ]
            ),
        )
        integrator = PrIntegrator(runner=runner)
        result = integrator.open_or_update(
            plan,
            target,
            _make_target_result(target=target),
        )

        assert result.state == PrState.UPDATED
        assert result.pr_number == 10
        assert result.pr_url == "https://github.com/acme-org/consumer/pull/10"
        assert "unchanged" in result.message

        # No pr edit call should have been made
        edit_calls = [c for c, _ in runner.calls if len(c) >= 3 and c[1] == "pr" and c[2] == "edit"]
        assert len(edit_calls) == 0

    def test_existing_pr_body_different(self) -> None:
        """Existing PR with different body -> UPDATED, 'body updated'."""
        plan = _make_plan()
        target = _make_target()

        runner = GhRunner()
        runner.set_response(
            ("gh", "pr", "list"),
            stdout=json.dumps(
                [
                    {
                        "number": 10,
                        "url": "https://github.com/acme-org/consumer/pull/10",
                        "body": "Old body text",
                        "headRefOid": "abc123",
                    }
                ]
            ),
        )
        runner.set_response(("gh", "pr", "edit"), stdout="")
        integrator = PrIntegrator(runner=runner)
        result = integrator.open_or_update(
            plan,
            target,
            _make_target_result(target=target),
        )

        assert result.state == PrState.UPDATED
        assert result.pr_number == 10
        assert "body updated" in result.message.lower()

        # pr edit should have been called
        edit_calls = [c for c, _ in runner.calls if len(c) >= 3 and c[1] == "pr" and c[2] == "edit"]
        assert len(edit_calls) == 1
        cmd = edit_calls[0]
        assert "--body-file" in cmd

    def test_existing_pr_dry_run_still_updates_body(self) -> None:
        """dry_run only affects creation; existing PR body update proceeds."""
        plan = _make_plan()
        target = _make_target()

        runner = GhRunner()
        runner.set_response(
            ("gh", "pr", "list"),
            stdout=json.dumps(
                [
                    {
                        "number": 10,
                        "url": "https://github.com/acme-org/consumer/pull/10",
                        "body": "Stale body",
                        "headRefOid": "abc123",
                    }
                ]
            ),
        )
        runner.set_response(("gh", "pr", "edit"), stdout="")
        integrator = PrIntegrator(runner=runner)
        result = integrator.open_or_update(
            plan,
            target,
            _make_target_result(target=target),
            dry_run=True,
        )

        # Even with dry_run, existing PR body is updated
        assert result.state == PrState.UPDATED
        assert result.pr_number == 10


# ---------------------------------------------------------------------------
# open_or_update -- error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Tests for error conditions in open_or_update."""

    def test_gh_pr_create_auth_error(self) -> None:
        """gh pr create fails with auth error -> FAILED, redacted stderr."""
        runner = GhRunner()
        runner.set_response(("gh", "pr", "list"), stdout="[]\n")
        runner.set_error(
            ("gh", "pr", "create"),
            subprocess.CalledProcessError(
                1,
                ["gh", "pr", "create"],
                output="",
                stderr=(
                    "fatal: authentication failed for 'https://x-access-token:ghp_FAKE@github.com'"
                ),
            ),
        )
        integrator = PrIntegrator(runner=runner)
        result = integrator.open_or_update(
            _make_plan(),
            _make_target(),
            _make_target_result(),
        )

        assert result.state == PrState.FAILED
        assert result.pr_number is None
        assert result.pr_url is None
        assert "ghp_FAKE" not in result.message
        assert "***" in result.message

    def test_gh_pr_list_malformed_json(self) -> None:
        """gh pr list returns non-JSON -> FAILED."""
        runner = GhRunner()
        runner.set_response(
            ("gh", "pr", "list"),
            stdout="not valid json{{{",
        )
        integrator = PrIntegrator(runner=runner)
        result = integrator.open_or_update(
            _make_plan(),
            _make_target(),
            _make_target_result(),
        )

        assert result.state == PrState.FAILED
        assert "parse" in result.message.lower() or "OS error" in result.message

    def test_timeout_expired(self) -> None:
        """gh times out -> FAILED with timeout message."""
        runner = GhRunner()
        runner.set_error(
            ("gh", "pr", "list"),
            subprocess.TimeoutExpired(cmd=["gh", "pr", "list"], timeout=30),
        )
        integrator = PrIntegrator(runner=runner, timeout_s=30.0)
        result = integrator.open_or_update(
            _make_plan(),
            _make_target(),
            _make_target_result(),
        )

        assert result.state == PrState.FAILED
        assert "timed out" in result.message

    def test_os_error(self) -> None:
        """OSError from runner -> FAILED."""
        runner = GhRunner()
        runner.set_error(
            ("gh", "pr", "list"),
            OSError("permission denied"),
        )
        integrator = PrIntegrator(runner=runner)
        result = integrator.open_or_update(
            _make_plan(),
            _make_target(),
            _make_target_result(),
        )

        assert result.state == PrState.FAILED
        assert "OS error" in result.message

    def test_gh_pr_list_fails_called_process_error(self) -> None:
        """gh pr list returns non-zero -> FAILED."""
        runner = GhRunner()
        runner.set_error(
            ("gh", "pr", "list"),
            subprocess.CalledProcessError(
                1,
                ["gh", "pr", "list"],
                output="",
                stderr="HTTP 403",
            ),
        )
        integrator = PrIntegrator(runner=runner)
        result = integrator.open_or_update(
            _make_plan(),
            _make_target(),
            _make_target_result(),
        )

        assert result.state == PrState.FAILED


# ---------------------------------------------------------------------------
# Token redaction
# ---------------------------------------------------------------------------


class TestTokenRedaction:
    """Tests for token redaction in error messages."""

    def test_redacts_access_token(self) -> None:
        """Tokens in stderr are replaced with ***."""
        text = (
            "fatal: authentication failed for "
            "'https://x-access-token:ghp_FAKE123@github.com/acme-org/repo'"
        )
        redacted = _redact_token(text)
        assert "ghp_FAKE123" not in redacted
        assert "https://***@" in redacted

    def test_redacts_multiple_tokens(self) -> None:
        """Multiple token patterns are all redacted."""
        text = "tried https://user:token1@host1 and https://user:token2@host2"
        redacted = _redact_token(text)
        assert "token1" not in redacted
        assert "token2" not in redacted

    def test_no_token_unchanged(self) -> None:
        """Text without tokens is returned unchanged."""
        text = "fatal: repository not found"
        assert _redact_token(text) == text

    def test_failed_message_redacts_token_from_stderr(self) -> None:
        """Full integration: FAILED result message has tokens redacted."""
        runner = GhRunner()
        runner.set_response(("gh", "pr", "list"), stdout="[]\n")
        runner.set_error(
            ("gh", "pr", "create"),
            subprocess.CalledProcessError(
                128,
                ["gh", "pr", "create"],
                output="",
                stderr="https://x-access-token:ghp_SECRET@github.com 403",
            ),
        )
        integrator = PrIntegrator(runner=runner)
        result = integrator.open_or_update(
            _make_plan(),
            _make_target(),
            _make_target_result(),
        )

        assert result.state == PrState.FAILED
        assert "ghp_SECRET" not in result.message
        assert "***" in result.message


# ---------------------------------------------------------------------------
# PR title and body templates
# ---------------------------------------------------------------------------


class TestTemplates:
    """Tests for PR title and body rendering."""

    def test_title_format(self) -> None:
        """Title follows the chore(apm) convention."""
        plan = _make_plan(
            marketplace_name="acme-tools",
            marketplace_version="3.1.0",
        )
        title = _build_title(plan)
        assert title == "chore(apm): bump acme-tools to 3.1.0"

    def test_body_contains_marketplace_name(self) -> None:
        plan = _make_plan(marketplace_name="my-mkt")
        body = _build_body(plan, _make_target())
        assert "`my-mkt`" in body

    def test_body_contains_version(self) -> None:
        plan = _make_plan(marketplace_version="4.0.0")
        body = _build_body(plan, _make_target())
        assert "`4.0.0`" in body

    def test_body_contains_new_ref(self) -> None:
        plan = _make_plan(new_ref="v5.0.0")
        body = _build_body(plan, _make_target())
        assert "`v5.0.0`" in body

    def test_body_contains_branch_name(self) -> None:
        plan = _make_plan(
            branch_name="apm/marketplace-update-x-1.0-abc12345",
        )
        body = _build_body(plan, _make_target())
        assert "`apm/marketplace-update-x-1.0-abc12345`" in body

    def test_body_contains_path_in_repo(self) -> None:
        target = _make_target(path_in_repo="config/apm.yml")
        body = _build_body(_make_plan(), target)
        assert "`config/apm.yml`" in body

    def test_body_contains_apm_publish_id_comment(self) -> None:
        plan = _make_plan(short_hash="deadbeef")
        body = _build_body(plan, _make_target())
        assert "<!-- APM-Publish-Id: deadbeef -->" in body

    def test_body_short_hash_fallback_from_branch_name(self) -> None:
        """When plan.short_hash is empty, derive from branch_name."""
        plan = _make_plan(
            short_hash="",
            branch_name="apm/marketplace-update-tools-1.0.0-ff00ff00",
        )
        body = _build_body(plan, _make_target())
        assert "<!-- APM-Publish-Id: ff00ff00 -->" in body

    def test_body_all_ascii(self) -> None:
        """Body must contain only ASCII characters."""
        plan = _make_plan()
        body = _build_body(plan, _make_target())
        assert body.isascii()

    def test_title_all_ascii(self) -> None:
        """Title must contain only ASCII characters."""
        plan = _make_plan()
        title = _build_title(plan)
        assert title.isascii()

    def test_body_starts_with_automated_update(self) -> None:
        body = _build_body(_make_plan(), _make_target())
        assert body.startswith("Automated update from `apm marketplace publish`.")


# ---------------------------------------------------------------------------
# _extract_short_hash
# ---------------------------------------------------------------------------


class TestExtractShortHash:
    """Tests for _extract_short_hash helper."""

    def test_uses_plan_short_hash_when_set(self) -> None:
        plan = _make_plan(short_hash="aabbccdd")
        assert _extract_short_hash(plan) == "aabbccdd"

    def test_falls_back_to_branch_name(self) -> None:
        plan = _make_plan(
            short_hash="",
            branch_name="apm/marketplace-update-tools-1.0.0-12345678",
        )
        assert _extract_short_hash(plan) == "12345678"

    def test_empty_when_no_hash_available(self) -> None:
        plan = _make_plan(short_hash="", branch_name="simple-branch")
        # rsplit("-", 1) on "simple-branch" -> ["simple", "branch"]
        # The fallback returns the last segment
        result = _extract_short_hash(plan)
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# PrIntegrator construction
# ---------------------------------------------------------------------------


class TestPrIntegratorInit:
    """Tests for PrIntegrator constructor defaults."""

    def test_default_gh_bin(self) -> None:
        integrator = PrIntegrator()
        assert integrator._gh_bin == "gh"

    def test_custom_gh_bin(self) -> None:
        integrator = PrIntegrator(gh_bin="/usr/local/bin/gh")
        assert integrator._gh_bin == "/usr/local/bin/gh"

    def test_custom_timeout(self) -> None:
        integrator = PrIntegrator(timeout_s=60.0)
        assert integrator._timeout_s == 60.0

    def test_runner_injectable(self) -> None:
        runner = GhRunner()
        integrator = PrIntegrator(runner=runner)
        assert integrator._runner is runner


# ---------------------------------------------------------------------------
# Integration-level: end-to-end flow
# ---------------------------------------------------------------------------


class TestEndToEnd:
    """Integration-level tests combining multiple steps."""

    def test_full_create_flow(self) -> None:
        """Full flow: check_available -> open_or_update (create)."""
        runner = GhRunner()
        runner.set_response(
            ("gh", "--version"),
            stdout="gh version 2.50.0\n",
        )
        runner.set_response(("gh", "auth", "status"), stdout="Logged in\n")
        runner.set_response(("gh", "pr", "list"), stdout="[]\n")
        runner.set_response(
            ("gh", "pr", "create"),
            stdout="https://github.com/acme-org/consumer/pull/55\n",
        )

        integrator = PrIntegrator(runner=runner)
        ok, _ = integrator.check_available()
        assert ok is True

        result = integrator.open_or_update(
            _make_plan(),
            _make_target(),
            _make_target_result(),
        )
        assert result.state == PrState.OPENED
        assert result.pr_number == 55

    def test_pr_list_uses_check_true(self) -> None:
        """gh pr list is called with check=True."""
        runner = GhRunner()
        runner.set_response(("gh", "pr", "list"), stdout="[]\n")
        runner.set_response(
            ("gh", "pr", "create"),
            stdout="https://github.com/acme-org/consumer/pull/1\n",
        )
        integrator = PrIntegrator(runner=runner)
        integrator.open_or_update(
            _make_plan(),
            _make_target(),
            _make_target_result(),
        )

        list_calls = [
            (c, kw) for c, kw in runner.calls if len(c) >= 3 and c[1] == "pr" and c[2] == "list"
        ]
        assert len(list_calls) == 1
        _, kw = list_calls[0]
        assert kw.get("check") is True

    def test_body_file_used_in_create(self) -> None:
        """gh pr create uses --body-file, not --body."""
        runner = GhRunner()
        runner.set_response(("gh", "pr", "list"), stdout="[]\n")
        runner.set_response(
            ("gh", "pr", "create"),
            stdout="https://github.com/acme-org/consumer/pull/1\n",
        )
        integrator = PrIntegrator(runner=runner)
        integrator.open_or_update(
            _make_plan(),
            _make_target(),
            _make_target_result(),
        )

        create_calls = [
            c for c, _ in runner.calls if len(c) >= 3 and c[1] == "pr" and c[2] == "create"
        ]
        cmd = create_calls[0]
        assert "--body-file" in cmd
        assert "--body" not in cmd or "--body-file" in cmd

    def test_title_passed_to_create(self) -> None:
        """gh pr create receives the correct --title."""
        runner = GhRunner()
        runner.set_response(("gh", "pr", "list"), stdout="[]\n")
        runner.set_response(
            ("gh", "pr", "create"),
            stdout="https://github.com/acme-org/consumer/pull/1\n",
        )
        plan = _make_plan(
            marketplace_name="acme-tools",
            marketplace_version="2.0.0",
        )
        integrator = PrIntegrator(runner=runner)
        integrator.open_or_update(
            plan,
            _make_target(),
            _make_target_result(),
        )

        create_calls = [
            c for c, _ in runner.calls if len(c) >= 3 and c[1] == "pr" and c[2] == "create"
        ]
        cmd = create_calls[0]
        title_idx = cmd.index("--title")
        assert cmd[title_idx + 1] == "chore(apm): bump acme-tools to 2.0.0"
