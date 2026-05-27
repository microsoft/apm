"""End-to-end tests for ``policy.dependencies.require_pinned_constraint``.

Follow-up to #1494. The original PR shipped unit tests plus a partial
e2e file (``tests/integration/test_policy_pinned_constraint_e2e.py``)
that covered the block-aborts and warn-emits paths only. This module
fills the remaining promise gaps surfaced by the test-coverage-expert
lens:

- Promise B: ``apm install`` proceeds when every direct dep is pinned
  even under ``enforcement: block`` + ``require_pinned_constraint: true``.
- Promise C: the ``direct_dep_keys`` wiring (policy_gate -> runner)
  is preserved, so the policy gate never flags transitive deps.
- Promise D + G: when block fires, exit code is 1 and the diagnostic
  cites both the offending dep ref and a pinning-hint phrase a user
  can act on.
- Promise E: backward compat -- ``require_pinned_constraint: false``
  (the default) does NOT block an unbounded dep, even under
  ``enforcement: block``.
- Promise F: ``--dry-run`` previews the violation with a "Would be
  blocked by policy" line and exits 0 without writing a lockfile.

The full pipeline is exercised via ``CliRunner.invoke(cli, ...)`` with
discovery + downloader mocked at well-known seams (the only network
boundaries). No CLI runtime internals are mocked.

ASCII-only per .github/instructions/encoding.instructions.md.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.policy.discovery import PolicyFetchResult
from apm_cli.policy.parser import load_policy
from apm_cli.policy.schema import ApmPolicy, DependencyPolicy

FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures" / "policy" / "require_pinned"

_PATCH_DISCOVER_GATE = "apm_cli.policy.discovery.discover_policy_with_chain"
_PATCH_DISCOVER_PREFLIGHT = "apm_cli.policy.install_preflight.discover_policy_with_chain"
_PATCH_UPDATES = "apm_cli.commands._helpers.check_for_updates"
_PATCH_DOWNLOADER = "apm_cli.deps.github_downloader.GitHubPackageDownloader"
# The gate phase imports ``run_dependency_policy_checks`` lazily inside
# ``run()`` (see policy_gate.py), so we patch the source module. The
# preflight path imports it differently; the gate is the seam that owns
# the direct_dep_keys derivation, so capturing every call to the runner
# and filtering to the one originating from the gate is sufficient.
_PATCH_RUN_DEP_CHECKS = "apm_cli.policy.policy_checks.run_dependency_policy_checks"


def _fetch(policy: ApmPolicy) -> PolicyFetchResult:
    return PolicyFetchResult(
        policy=policy,
        source="org:test-org/.github",
        cached=False,
        error=None,
        cache_age_seconds=None,
        cache_stale=False,
        fetch_error=None,
        outcome="found",
    )


def _write_apm_yml(path: Path, deps: list) -> None:
    data = {
        "name": "test-project",
        "version": "1.0.0",
        "dependencies": {"apm": deps},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")


def _policy(*, enforcement: str = "block", required: bool = True) -> ApmPolicy:
    return ApmPolicy(
        enforcement=enforcement,
        dependencies=DependencyPolicy(require_pinned_constraint=required),
    )


def _load_fixture(name: str) -> ApmPolicy:
    path = FIXTURES_DIR / name
    assert path.exists(), f"Fixture not found: {path}"
    policy, _ = load_policy(path)
    return policy


def _invoke_install(runner: CliRunner, args: list | None = None):
    from apm_cli.cli import cli

    return runner.invoke(cli, ["install"] + (args or []))


@pytest.fixture()
def project(tmp_path):
    """Create a minimal project layout and chdir into it."""
    orig_cwd = os.getcwd()
    project_dir = tmp_path / "require-pinned-e2e"
    project_dir.mkdir()
    (project_dir / ".github").mkdir()
    (project_dir / ".github" / "copilot-instructions.md").write_text("# test\n")
    os.chdir(project_dir)
    yield project_dir, CliRunner()
    os.chdir(orig_cwd)


# ---------------------------------------------------------------------------
# Promise B: pinned dep proceeds past the policy gate
# ---------------------------------------------------------------------------


class TestPromiseBPinnedDepPassesPolicyGate:
    """When every direct dep is pinned, the gate emits NO pinned-constraint
    violation under enforcement=block + require_pinned_constraint=true.

    We assert on the absence of the policy-block diagnostic rather than
    on install-success, because the downloader is mocked and resolution
    may not produce a real lockfile. The behavioral promise is "policy
    does not abort here" -- that is what we pin down.
    """

    @patch(_PATCH_UPDATES, return_value=None)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_DISCOVER_PREFLIGHT)
    @patch(_PATCH_DISCOVER_GATE)
    def test_caret_range_does_not_trigger_block(
        self, mock_gate, mock_preflight, mock_dl, mock_updates, project
    ):
        project_dir, runner = project
        _write_apm_yml(
            project_dir / "apm.yml",
            deps=["test-org/skills#^1.2.0", "other/lib#~2.0.0"],
        )
        fetch = _fetch(_load_fixture("apm-policy-block.yml"))
        mock_gate.return_value = fetch
        mock_preflight.return_value = fetch

        result = _invoke_install(runner)

        out = result.output.lower()
        assert "blocked by org policy" not in out, (
            f"Pinned deps must not trigger policy block:\n{result.output}"
        )
        assert "unbounded constraint" not in out, (
            f"Pinned deps must not produce unbounded-constraint diagnostic:\n{result.output}"
        )

    @patch(_PATCH_UPDATES, return_value=None)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_DISCOVER_PREFLIGHT)
    @patch(_PATCH_DISCOVER_GATE)
    def test_bare_exact_version_does_not_trigger_block(
        self, mock_gate, mock_preflight, mock_dl, mock_updates, project
    ):
        # Covers both pin shapes the classifier accepts:
        #   * ``1.2.3``       -- bare exact version
        #   * ``=1.2.3``      -- npm/cargo-style explicit-equality
        # Both must pass the gate under enforcement=block +
        # require_pinned_constraint=true. The ``=1.2.3`` half is the
        # regression trap for the bug observed in PR #1505: before the
        # fix, ``_constraint_pinning.py`` mis-classified ``=1.2.3`` as
        # ``BARE_BRANCH`` and blocked the install.
        project_dir, runner = project
        _write_apm_yml(
            project_dir / "apm.yml",
            deps=["test-org/skills#1.2.3", "test-org/other#=1.2.3"],
        )
        fetch = _fetch(_load_fixture("apm-policy-block.yml"))
        mock_gate.return_value = fetch
        mock_preflight.return_value = fetch

        result = _invoke_install(runner)

        out = result.output.lower()
        assert "blocked by org policy" not in out, result.output
        assert "unbounded constraint" not in out, result.output
        assert "bare branch" not in out, result.output


# ---------------------------------------------------------------------------
# Promise C: direct_dep_keys wiring (transitive bleed-through guard)
# ---------------------------------------------------------------------------


class TestPromiseCDirectDepKeysWiring:
    """Regression trap for the #1494 Copilot follow-up: the gate phase
    MUST forward ``direct_dep_keys`` to ``run_dependency_policy_checks``
    so the pinned-constraint check restricts itself to direct deps and
    cannot be tripped by an unbounded transitive a consumer can't fix.

    This e2e exercises the wiring by capturing the kwargs passed to the
    runner from inside ``policy_gate.run()``. A regression that drops
    the kwarg surfaces immediately.
    """

    @patch(_PATCH_UPDATES, return_value=None)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_DISCOVER_PREFLIGHT)
    @patch(_PATCH_DISCOVER_GATE)
    @patch(_PATCH_RUN_DEP_CHECKS)
    def test_policy_gate_forwards_direct_dep_keys_to_runner(
        self,
        mock_run_checks,
        mock_gate,
        mock_preflight,
        mock_dl,
        mock_updates,
        project,
    ):
        project_dir, runner = project
        _write_apm_yml(
            project_dir / "apm.yml",
            deps=["test-org/skills#^1.2.0"],
        )
        fetch = _fetch(_load_fixture("apm-policy-block.yml"))
        mock_gate.return_value = fetch
        mock_preflight.return_value = fetch

        # Return a passing audit so call sites proceed without raising.
        from apm_cli.policy.models import CIAuditResult

        mock_run_checks.return_value = CIAuditResult(checks=[])

        _invoke_install(runner)

        # ``run_dependency_policy_checks`` may be invoked from preflight,
        # gate, and post-target-check; we assert at least one such call
        # forwarded a non-empty ``direct_dep_keys`` set referencing the
        # declared direct dep. A regression that drops the kwarg at the
        # gate produces calls with ``direct_dep_keys=None`` (or missing)
        # and fails this assertion.
        forwarded = [c for c in mock_run_checks.call_args_list if c.kwargs.get("direct_dep_keys")]
        assert forwarded, (
            "No call to run_dependency_policy_checks forwarded a non-empty "
            "direct_dep_keys. The #1494 Copilot-follow-up transitive guard "
            f"has regressed. Calls: {mock_run_checks.call_args_list}"
        )
        # The key must reference the declared direct dep on at least one
        # of the forwarded calls.
        joined = " ".join(str(k) for call in forwarded for k in call.kwargs["direct_dep_keys"])
        assert "test-org/skills" in joined, (
            f"direct_dep_keys does not contain the declared direct dep "
            f"on any forwarding call; joined keys: {joined!r}"
        )


# ---------------------------------------------------------------------------
# Promise D + G: actionable diagnostic + exit code 1
# ---------------------------------------------------------------------------


class TestPromiseDGBlockDiagnosticAndExitCode:
    """When block fires:
    - Exit code is 1 (the install-failure code documented by every
      ``sys.exit(1)`` in commands/install.py for non-usage errors).
    - The diagnostic cites the offending dep ref AND a pinning hint
      the user can act on. The hint comes from
      ``_check_pinned_constraints`` (``policy_checks.py``).
    """

    @patch(_PATCH_UPDATES, return_value=None)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_DISCOVER_PREFLIGHT)
    @patch(_PATCH_DISCOVER_GATE)
    def test_block_exits_one_and_diagnostic_is_actionable(
        self, mock_gate, mock_preflight, mock_dl, mock_updates, project
    ):
        project_dir, runner = project
        # WILDCARD reason -> humanized as 'wildcard' per humanize_reason.
        _write_apm_yml(
            project_dir / "apm.yml",
            deps=["test-org/skills#*"],
        )
        fetch = _fetch(_load_fixture("apm-policy-block.yml"))
        mock_gate.return_value = fetch
        mock_preflight.return_value = fetch

        result = _invoke_install(runner)

        assert result.exit_code == 1, (
            f"Policy block should exit 1, got {result.exit_code}\n{result.output}"
        )
        out = result.output
        out_lower = out.lower()
        # The check name surfaces so policy authors can grep their
        # apm.policy.yml for the relevant field.
        assert "dependency-pinned-constraint" in out_lower, (
            f"Diagnostic must surface the check name so the user can "
            f"locate the policy field that fired:\n{out}"
        )
        # The "pin to a semver range, literal tag, or SHA" hint comes
        # from the check message and is the actionable bit a user can
        # paste back into apm.yml.
        assert "pin" in out_lower, f"Diagnostic must include a pinning hint:\n{out}"
        # The dep ref MUST appear inside the violation block itself --
        # not merely as part of an earlier resolver line. Locate the
        # violation block and assert on its content.
        idx = out_lower.find("dependency-pinned-constraint")
        violation_block = out[idx : idx + 600]
        assert "test-org/skills" in violation_block, (
            f"Diagnostic must cite the offending dep INSIDE the violation "
            f"block (not just in resolver noise above):\n{violation_block}"
        )


# ---------------------------------------------------------------------------
# Promise E: backward compat -- require_pinned_constraint=false (default)
# ---------------------------------------------------------------------------


class TestPromiseERequirePinnedFalseDoesNotBlock:
    """When ``require_pinned_constraint`` is false (the default), an
    unbounded dep MUST NOT trigger a policy block even at
    ``enforcement: block``. This guards the backward-compat path: any
    policy file written before #1494 lands keeps the old behavior.
    """

    @patch(_PATCH_UPDATES, return_value=None)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_DISCOVER_PREFLIGHT)
    @patch(_PATCH_DISCOVER_GATE)
    def test_unbounded_dep_passes_when_field_disabled(
        self, mock_gate, mock_preflight, mock_dl, mock_updates, project
    ):
        project_dir, runner = project
        # Same unbounded dep that block-mode would otherwise reject.
        _write_apm_yml(
            project_dir / "apm.yml",
            deps=["test-org/skills"],  # NO_REF
        )
        fetch = _fetch(_load_fixture("apm-policy-off.yml"))
        mock_gate.return_value = fetch
        mock_preflight.return_value = fetch

        result = _invoke_install(runner)

        out = result.output.lower()
        assert "unbounded constraint" not in out, (
            f"Disabled require_pinned_constraint must not emit the "
            f"unbounded-constraint diagnostic:\n{result.output}"
        )
        assert "dependency-pinned-constraint" not in out or (
            "0 dependency" in out  # pure noise if check passes silently
        ), result.output


# ---------------------------------------------------------------------------
# Promise F: --dry-run previews the violation without mutating disk
# ---------------------------------------------------------------------------


class TestPromiseFDryRunPreviewsViolation:
    """``apm install --dry-run`` with an unbounded dep + block policy
    must:
    - exit 0 (dry-run never aborts on policy)
    - render a 'Would be blocked by policy' line citing the dep
    - NOT create apm.lock.yaml or apm_modules/
    """

    @patch(_PATCH_UPDATES, return_value=None)
    @patch(_PATCH_DISCOVER_PREFLIGHT)
    @patch(_PATCH_DISCOVER_GATE)
    def test_dry_run_previews_block_without_aborting_or_mutating(
        self, mock_gate, mock_preflight, mock_updates, project
    ):
        project_dir, runner = project
        _write_apm_yml(
            project_dir / "apm.yml",
            deps=["test-org/skills"],  # NO_REF, will trip the check
        )
        fetch = _fetch(_load_fixture("apm-policy-block.yml"))
        mock_gate.return_value = fetch
        mock_preflight.return_value = fetch

        result = _invoke_install(runner, ["--dry-run"])

        out = result.output
        assert result.exit_code == 0, f"Dry-run should exit 0, got {result.exit_code}\n{out}"
        # The preflight emits 'Would be blocked by policy: <dep> -- <reason>'.
        assert "would be blocked by policy" in out.lower(), (
            f"Dry-run must preview the block diagnostic:\n{out}"
        )
        assert "test-org/skills" in out, f"Preview must cite the offending dep:\n{out}"
        # No filesystem mutation.
        assert not (project_dir / "apm.lock.yaml").exists(), "Dry-run must NOT create a lockfile"
        assert not (project_dir / "apm_modules").exists(), "Dry-run must NOT create apm_modules/"
