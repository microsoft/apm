"""CLI/InstallCommand-tier coverage for the write-boundary re-check (#3129).

``HookIntegrator._integrate_merged_hooks`` unconditionally re-checks
authorized Cursor/Claude-import sources at the per-target write boundary,
even though ``preflight_hooks_for_targets`` already ran up front for the
same plan -- specifically to catch a Claude import that appears between
those two steps. ``tests/unit/integration/test_cursor_hook_native_contract.py``
already proves this holds at the ``HookIntegrator`` tier directly and at the
``services.integrate_package_primitives`` orchestration tier.

Neither of those goes through the real ``apm install`` CLI command, which
has its own argument parsing, error rendering, and exit-code mapping on top
of the orchestration call. This module closes that last tier: it drives the
REAL ``apm install`` Click command end-to-end (``CliRunner``, no internals
called directly), stubbing only the network download seam
(``GitHubPackageDownloader.download_package``) -- matching the hermetic
pattern already used by ``tests/integration/test_hook_wipe_target_scope_e2e.py``
and ``tests/integration/test_prune_hook_reconciliation_e2e.py``.
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.integration import hook_integrator
from apm_cli.integration.hook_integrator import HookIntegrator
from apm_cli.models.apm_package import (
    APMPackage,
    GitReferenceType,
    PackageInfo,
    ResolvedReference,
    clear_apm_yml_cache,
)
from apm_cli.models.dependency.reference import DependencyReference

pytestmark = [pytest.mark.integration]

_PATCH_UPDATES = "apm_cli.commands._helpers.check_for_updates"
_DEP_REPO_URL = "acme/cursor-gate"


def _stub_download_package(_self, repo_ref, install_path, *_args, **_kwargs):
    """Materialize a hermetic local package with one Cursor-compatible hook."""
    dep_ref = (
        repo_ref
        if isinstance(repo_ref, DependencyReference)
        else DependencyReference.parse(str(repo_ref))
    )
    install_path = Path(install_path)
    install_path.mkdir(parents=True, exist_ok=True)
    pkg_name = dep_ref.repo_url.rsplit("/", maxsplit=1)[-1]
    (install_path / "apm.yml").write_text(
        yaml.safe_dump(
            {"name": pkg_name, "version": "1.0.0", "description": "Hermetic CLI-tier fixture"},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    hooks_dir = install_path / ".apm" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "gate.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": "echo shared"}],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    package = APMPackage.from_apm_yml(install_path / "apm.yml")
    return PackageInfo(
        package=package,
        install_path=install_path,
        installed_at=datetime.now().isoformat(),
        dependency_ref=dep_ref,
        resolved_reference=ResolvedReference(
            original_ref="main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit=None,
            ref_name="main",
        ),
    )


def _write_project(project: Path) -> None:
    project.mkdir(parents=True, exist_ok=True)
    (project / "apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": "cli-tier-consumer",
                "version": "1.0.0",
                "targets": ["cursor"],
                "dependencies": {"apm": [_DEP_REPO_URL]},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    clear_apm_yml_cache()


def _run_install(project: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(project)
    with (
        patch(_PATCH_UPDATES, return_value=None),
        patch.object(
            GitHubPackageDownloader,
            "download_package",
            autospec=True,
            side_effect=_stub_download_package,
        ),
    ):
        return CliRunner().invoke(cli, ["install", "--no-policy"], catch_exceptions=False)


def _run_install_with_late_claude_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    *,
    disable_per_write_recheck: bool = False,
):
    """Run one real ``apm install`` CLI invocation, injecting a conflicting
    Claude import between the CLI's real up-front preflight and its real
    per-target write (both inside that single real command).

    When ``disable_per_write_recheck`` is set, the per-write re-check inside
    ``HookIntegrator._integrate_merged_hooks`` is disabled for the duration
    of that one real method call ONLY -- the up-front
    ``preflight_hooks_for_targets`` call (which happens earlier, including
    its own internal real ``preflight_cursor_hooks`` call) always runs for
    real and is never touched by this scoping. This mirrors the
    service-tier mutation proof in
    ``tests/unit/integration/test_cursor_hook_native_contract.py`` at the
    CLI tier, so the same "which call enforces the guarantee" question is
    answered through the real command, not just the orchestration call.
    """
    project = tmp_path / label / "project"
    _write_project(project)

    real_preflight = HookIntegrator.preflight_hooks_for_targets
    claude_path = project / ".claude/settings.json"
    injected_claude_bytes = (
        json.dumps(
            {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "echo shared"}]}]}}
        )
        + "\n"
    ).encode("utf-8")

    def _preflight_then_inject_claude_import(self, *args, **kwargs):
        real_preflight(self, *args, **kwargs)
        claude_path.parent.mkdir(parents=True, exist_ok=True)
        claude_path.write_bytes(injected_claude_bytes)

    real_integrate_merged = HookIntegrator._integrate_merged_hooks

    def _integrate_merged_with_recheck_disabled(self, *args, **kwargs):
        original = hook_integrator.preflight_cursor_hooks
        hook_integrator.preflight_cursor_hooks = lambda *a, **k: None
        try:
            return real_integrate_merged(self, *args, **kwargs)
        finally:
            hook_integrator.preflight_cursor_hooks = original

    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(
                HookIntegrator, "preflight_hooks_for_targets", _preflight_then_inject_claude_import
            )
        )
        if disable_per_write_recheck:
            stack.enter_context(
                patch.object(
                    HookIntegrator,
                    "_integrate_merged_hooks",
                    _integrate_merged_with_recheck_disabled,
                )
            )
        result = _run_install(project, monkeypatch)

    return result, project, claude_path, injected_claude_bytes


def _assert_cli_rejected_the_late_import(
    result, project: Path, claude_path: Path, injected_bytes: bytes
) -> None:
    """The shared regression assertion, reused unmodified against a real run
    and a scoped-mutant run so the mutant proof exercises the exact same
    check the baseline must satisfy.

    Rich wraps CLI error output to the CI runner's (often narrower,
    non-TTY) detected console width, which can split the literal "Claude
    import" substring across a line break. Collapse all whitespace runs
    (including embedded wrap newlines) before the substring check, matching
    this PR's existing narrow-console normalization, so the assertion
    verifies the full semantic phrase regardless of wrap point.
    """
    normalized_output = " ".join(result.output.split())
    assert result.exit_code != 0, (
        "expected a non-zero exit for a Claude import appearing mid-install; "
        f"got 0 (missing refusal). output={normalized_output!r}"
    )
    assert "Claude import" in normalized_output, normalized_output
    assert not (project / ".cursor/hooks.json").exists(), (
        "native write must not occur when the rejection fires"
    )
    assert claude_path.read_bytes() == injected_bytes


def test_cli_install_rejects_claude_import_appearing_mid_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Claude import appearing between the CLI's real up-front preflight
    and its real per-target write must be refused by the real command, with
    the native target untouched and the injected import's bytes unchanged.
    """
    result, project, claude_path, injected_bytes = _run_install_with_late_claude_import(
        tmp_path, monkeypatch, "baseline"
    )
    _assert_cli_rejected_the_late_import(result, project, claude_path, injected_bytes)


def test_cli_install_writer_only_disable_loses_the_rejection_then_restores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation control at the CLI tier: disabling ONLY the per-write
    re-check inside ``_integrate_merged_hooks`` (not the up-front preflight,
    which keeps running for real) must make the SAME regression assertion
    above FAIL through the real ``apm install`` command, for the expected
    missing-refusal reason -- then restoring the real guard must make a
    fresh real run pass again.
    """
    # Baseline sanity check first, with every real guard intact.
    result, project, claude_path, injected_bytes = _run_install_with_late_claude_import(
        tmp_path, monkeypatch, "before"
    )
    _assert_cli_rejected_the_late_import(result, project, claude_path, injected_bytes)

    # Scoped mutant: only the per-write recheck is disabled, for the
    # duration of that one real method call. The real CLI's up-front
    # preflight still runs for real.
    mutant_result, mutant_project, mutant_claude_path, mutant_injected_bytes = (
        _run_install_with_late_claude_import(
            tmp_path, monkeypatch, "mutant", disable_per_write_recheck=True
        )
    )
    with pytest.raises(AssertionError, match="missing refusal"):
        _assert_cli_rejected_the_late_import(
            mutant_result, mutant_project, mutant_claude_path, mutant_injected_bytes
        )
    # Confirm the assertion failed for the expected reason (the real CLI
    # command exited successfully and wrote the native target), not some
    # unrelated break.
    assert mutant_result.exit_code == 0, mutant_result.output
    assert (mutant_project / ".cursor/hooks.json").exists()

    # Restored: the real guard is back in place for the next real install.
    restored_result, restored_project, restored_claude_path, restored_injected_bytes = (
        _run_install_with_late_claude_import(tmp_path, monkeypatch, "restored")
    )
    _assert_cli_rejected_the_late_import(
        restored_result, restored_project, restored_claude_path, restored_injected_bytes
    )


def test_cli_install_without_a_late_import_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nonconflicting control: the same real CLI install, with no import
    injected, must still succeed and write the native Cursor target -- this
    rules out the refusal above being a false positive baked into the
    fixture rather than a real reaction to the injected import.
    """
    project = tmp_path / "project"
    _write_project(project)

    result = _run_install(project, monkeypatch)

    assert result.exit_code == 0, result.output
    assert (project / ".cursor/hooks.json").exists()
