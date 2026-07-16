"""Hermetic E2E coverage for #2250: hook wipe/rebuild target-scope symmetry.

``HookIntegrator.sync_integration()`` is the shared "clear merged-hook
JSON entries" primitive both ``apm uninstall`` (``_sync_integrations_after_
uninstall`` in ``commands/uninstall/engine.py``) and ``apm prune``
(``HookIntegrator.reconcile_after_removal``, added in #2249) call as the
first half of a clear-then-rebuild transaction. Before this fix, the wipe
half always covered every ``KNOWN_TARGETS`` entry (by omitting
``targets=``), while the rebuild half only ever repopulated
``resolve_targets(explicit_target=apm_package.canonical_targets or None)``
-- the project's *currently declared* ``targets:`` list.

If a project's ``targets:`` list is narrowed (e.g. ``[claude, cursor]`` ->
``[claude]``) while a dependency stays installed and its old harness's
merged-hook file (``.cursor/hooks.json``) still exists on disk, the next
``apm uninstall`` or ``apm prune`` run wiped that harness's entire
``_apm_source``-tagged entry set and deleted its ``apm-hooks.json``
sidecar -- including entries for packages that are still declared and
still installed -- because the rebuild phase never revisited that
now-undeclared harness.

The fix scopes the wipe to the SAME resolved target set the rebuild uses
(see ``HookIntegrator.sync_integration``'s ``targets=`` parameter), for
BOTH callers. These tests drive the REAL ``apm install``, ``apm
uninstall``, and ``apm prune`` CLI commands end-to-end (Click
``CliRunner``, no internals called directly except where a test
specifically needs to prove an assertion is load-bearing), stubbing only
the network download seam (``GitHubPackageDownloader.download_package``),
matching the hermetic pattern already used by
``tests/integration/test_prune_hook_reconciliation_e2e.py``.

Matrix coverage is intentionally bounded to the two callers that share
the defective primitive (``uninstall``, ``prune``) crossed with the two
merge-hook schemas already established as semantically distinct in
``test_prune_hook_reconciliation_e2e.py`` (Claude's schema-strict inline
sidecar-only ownership vs Cursor's ``require_dir`` layout) -- a third
caller or harness would not exercise new code, only repeat assertions.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.deps.github_downloader import GitHubPackageDownloader
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

_TARGET_LAYOUT = {
    "claude": (".claude/settings.json", ".claude/apm-hooks.json"),
    "cursor": (".cursor/hooks.json", ".cursor/apm-hooks.json"),
}

# The two callers that route through HookIntegrator.sync_integration()'s
# clear-then-rebuild transaction and shared the pre-fix scope asymmetry.
_CALLERS = ["uninstall", "prune"]


def _stub_download_package(hook_commands: dict[str, str]):
    """Build a ``download_package`` stub that materializes a hooked fixture package."""

    def _download(
        _self: GitHubPackageDownloader,
        repo_ref: object,
        install_path: Path,
        *_args: object,
        **_kwargs: object,
    ) -> PackageInfo:
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
                {
                    "name": pkg_name,
                    "version": "1.0.0",
                    "description": f"Hermetic target-scope fixture: {pkg_name}",
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        command = hook_commands.get(dep_ref.repo_url)
        if command is not None:
            hooks_dir = install_path / ".apm" / "hooks"
            hooks_dir.mkdir(parents=True, exist_ok=True)
            (hooks_dir / "pre.json").write_text(
                json.dumps(
                    {
                        "hooks": {
                            "PreToolUse": [
                                {
                                    "matcher": "Bash",
                                    "hooks": [{"type": "command", "command": command}],
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

    return _download


def _write_project(project: Path, dep_repo_urls: list[str], targets: list[str]) -> None:
    project.mkdir(parents=True, exist_ok=True)
    (project / "apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": "hook-target-scope-consumer",
                "version": "1.0.0",
                "targets": targets,
                "dependencies": {"apm": dep_repo_urls},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    clear_apm_yml_cache()


def _narrow_targets(project: Path, targets: list[str]) -> None:
    """Drop the project's declared ``targets:`` list to *targets*, keeping deps."""
    manifest_path = project / "apm.yml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["targets"] = targets
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    clear_apm_yml_cache()


def _remove_dependency_from_manifest(project: Path, repo_url: str) -> None:
    """Drop *repo_url* from apm.yml's declared deps, keeping any siblings."""
    manifest_path = project / "apm.yml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    deps = manifest.get("dependencies", {}).get("apm", [])
    manifest["dependencies"]["apm"] = [d for d in deps if d != repo_url]
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    clear_apm_yml_cache()


def _run_cli(project: Path, monkeypatch: pytest.MonkeyPatch, args: list[str]) -> object:
    monkeypatch.chdir(project)
    with patch(_PATCH_UPDATES, return_value=None):
        return CliRunner().invoke(cli, args, catch_exceptions=False)


def _run_install(
    project: Path, monkeypatch: pytest.MonkeyPatch, hook_commands: dict[str, str]
) -> object:
    with patch.object(
        GitHubPackageDownloader,
        "download_package",
        autospec=True,
        side_effect=_stub_download_package(hook_commands),
    ):
        return _run_cli(project, monkeypatch, ["install", "--no-policy"])


def _remove_package(
    caller: str, project: Path, monkeypatch: pytest.MonkeyPatch, repo_url: str
) -> object:
    """Remove *repo_url* via the caller under test.

    ``uninstall`` both edits apm.yml and removes the package in one CLI
    call. ``prune`` requires the manifest edit to happen first (prune only
    removes packages already absent from apm.yml).
    """
    if caller == "uninstall":
        return _run_cli(project, monkeypatch, ["uninstall", repo_url])
    if caller == "prune":
        _remove_dependency_from_manifest(project, repo_url)
        return _run_cli(project, monkeypatch, ["prune"])
    raise ValueError(f"unknown caller: {caller}")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _sidecar_sources(sidecar_path: Path) -> set[str]:
    """Every _apm_source marker recorded anywhere in the sidecar file."""
    if not sidecar_path.exists():
        return set()
    data = _read_json(sidecar_path)
    sources: set[str] = set()
    for entries in data.values():
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict) and entry.get("_apm_source"):
                    sources.add(entry["_apm_source"])
    return sources


def _pre_tool_use_commands(config_path: Path) -> list[str]:
    if not config_path.exists():
        return []
    data = _read_json(config_path)
    entries = data.get("hooks", {}).get("PreToolUse", [])
    commands = []
    for entry in entries:
        for handler in entry.get("hooks", []):
            if isinstance(handler, dict) and "command" in handler:
                commands.append(handler["command"])
    return commands


def _install_two_packages_two_targets(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Install pkg-a + pkg-b under [claude, cursor], both with merged hooks."""
    _write_project(project, ["acme/pkg-a", "acme/pkg-b"], ["claude", "cursor"])
    result = _run_install(
        project,
        monkeypatch,
        {"acme/pkg-a": "./scripts/pkg-a-hook.sh", "acme/pkg-b": "./scripts/pkg-b-hook.sh"},
    )
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("caller", _CALLERS)
def test_narrowing_targets_preserves_sibling_hooks_in_dropped_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caller: str
) -> None:
    """RED (pre-fix): narrowing targets: then removing an unrelated package
    wiped EVERY package's entries (including still-installed pkg-a's) from
    the dropped harness (cursor), and deleted its ownership sidecar.

    GREEN (post-fix): the still-declared, still-installed sibling's hooks
    and manual/unowned entries in the now-undeclared harness survive,
    because the wipe is scoped to the same resolved target set the
    rebuild uses (the currently-declared [claude] only) -- cursor is
    simply never touched by this operation.
    """
    project = tmp_path / f"proj-{caller}"
    _install_two_packages_two_targets(project, monkeypatch)

    cursor_settings = project / ".cursor" / "hooks.json"
    cursor_sidecar = project / ".cursor" / "apm-hooks.json"
    claude_settings = project / ".claude" / "settings.json"
    claude_sidecar = project / ".claude" / "apm-hooks.json"
    assert {"pkg-a", "pkg-b"} <= _sidecar_sources(cursor_sidecar), (
        "precondition: both packages merged into cursor"
    )
    assert {"pkg-a", "pkg-b"} <= _sidecar_sources(claude_sidecar), (
        "precondition: both packages merged into claude"
    )

    # Inject a manual, user-owned entry directly into cursor's native
    # file -- never in the sidecar, so it carries no _apm_source marker.
    # It must survive regardless of what happens to package-owned entries.
    cursor_data = _read_json(cursor_settings)
    cursor_data.setdefault("hooks", {}).setdefault("PreToolUse", []).append(
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo manual-cursor-hook"}]}
    )
    cursor_settings.write_text(json.dumps(cursor_data), encoding="utf-8")

    # Narrow targets: [claude, cursor] -> [claude]. Both deps stay installed.
    _narrow_targets(project, ["claude"])

    result = _remove_package(caller, project, monkeypatch, "acme/pkg-b")
    assert result.exit_code == 0, result.output

    assert not (project / "apm_modules" / "acme" / "pkg-b").exists()
    assert (project / "apm_modules" / "acme" / "pkg-a").is_dir(), (
        "sibling package declared under the narrowed targets: must survive"
    )

    # The now-undeclared harness (cursor) must be untouched entirely: its
    # config file, sidecar, package-owned entries (both packages), and the
    # manual entry must all still be present exactly as before.
    assert cursor_settings.exists(), "cursor config must not be deleted by a narrowed-away wipe"
    assert cursor_sidecar.exists(), "cursor's ownership sidecar must survive"
    assert {"pkg-a", "pkg-b"} <= _sidecar_sources(cursor_sidecar), (
        "neither package's cursor hook entry may be lost when cursor is out of scope"
    )
    assert "echo manual-cursor-hook" in _pre_tool_use_commands(cursor_settings), (
        "manual user-owned cursor hook must remain untouched"
    )

    # The currently-declared harness (claude) IS in scope: pkg-b's entry
    # must be reconciled out, pkg-a's must survive.
    claude_sources = _sidecar_sources(claude_sidecar)
    assert "pkg-b" not in claude_sources, "removed package's claude hook entry must be gone"
    assert "pkg-a" in claude_sources, "surviving sibling's claude hook entry must remain"
    claude_commands = _pre_tool_use_commands(claude_settings)
    assert "./scripts/pkg-a-hook.sh" in claude_commands
    assert "./scripts/pkg-b-hook.sh" not in claude_commands


@pytest.mark.parametrize("caller", _CALLERS)
def test_negative_twin_without_narrowing_still_cleans_removed_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caller: str
) -> None:
    """Negative twin: without narrowing, both harnesses stay in scope.

    Removing pkg-b while targets: still declares [claude, cursor] must
    still reconcile pkg-b's entries out of BOTH harnesses (not just the
    declared subset) -- this proves the fix does not regress the normal,
    still-in-scope wipe/rebuild case into a silent no-op.
    """
    project = tmp_path / f"proj-negative-{caller}"
    _install_two_packages_two_targets(project, monkeypatch)

    cursor_sidecar = project / ".cursor" / "apm-hooks.json"
    claude_sidecar = project / ".claude" / "apm-hooks.json"

    result = _remove_package(caller, project, monkeypatch, "acme/pkg-b")
    assert result.exit_code == 0, result.output

    for sidecar, label in ((cursor_sidecar, "cursor"), (claude_sidecar, "claude")):
        sources = _sidecar_sources(sidecar)
        assert "pkg-b" not in sources, f"pkg-b's {label} entry must be reconciled when in scope"
        assert "pkg-a" in sources, f"pkg-a's {label} entry must survive"


@pytest.mark.parametrize("caller", _CALLERS)
def test_target_scope_reconciliation_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caller: str
) -> None:
    """Running the same removal twice must not error or change state further."""
    project = tmp_path / f"proj-idempotent-{caller}"
    _install_two_packages_two_targets(project, monkeypatch)
    _narrow_targets(project, ["claude"])

    first = _remove_package(caller, project, monkeypatch, "acme/pkg-b")
    assert first.exit_code == 0, first.output

    cursor_sidecar = project / ".cursor" / "apm-hooks.json"
    claude_settings = project / ".claude" / "settings.json"
    claude_sidecar = project / ".claude" / "apm-hooks.json"
    state_after_first = (
        _read_json(claude_settings) if claude_settings.exists() else None,
        cursor_sidecar.exists(),
        _sidecar_sources(cursor_sidecar),
        _sidecar_sources(claude_sidecar),
    )

    # Second run: uninstall re-invoking the same package is a no-op/error
    # for `uninstall` (package already gone from apm.yml); prune with
    # nothing orphaned is a no-op. Both must succeed without further
    # mutating cursor or claude state.
    if caller == "uninstall":
        second = _run_cli(project, monkeypatch, ["prune"])
    else:
        second = _remove_package(caller, project, monkeypatch, "acme/pkg-b")
    assert second.exit_code == 0, second.output

    state_after_second = (
        _read_json(claude_settings) if claude_settings.exists() else None,
        cursor_sidecar.exists(),
        _sidecar_sources(cursor_sidecar),
        _sidecar_sources(claude_sidecar),
    )
    assert state_after_second == state_after_first, "second run must be a no-op"


def test_zero_write_abort_before_destructive_wipe_on_prune(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prune's reconcile computes targets/dependencies BEFORE the wipe.

    If target resolution raises, nothing must be written -- a failed
    resolution must never leave a committed wipe with no rebuild to
    recover from (a zero-hook window until the next `apm install`).
    """
    project = tmp_path / "proj-zero-write"
    _install_two_packages_two_targets(project, monkeypatch)

    claude_settings = project / ".claude" / "settings.json"
    claude_sidecar = project / ".claude" / "apm-hooks.json"
    cursor_settings = project / ".cursor" / "hooks.json"
    cursor_sidecar = project / ".cursor" / "apm-hooks.json"
    before = {
        p: _read_json(p) if p.exists() else None
        for p in (claude_settings, claude_sidecar, cursor_settings, cursor_sidecar)
    }

    _remove_dependency_from_manifest(project, "acme/pkg-b")
    with patch(
        "apm_cli.integration.targets.resolve_targets",
        side_effect=RuntimeError("simulated target resolution failure"),
    ):
        result = _run_cli(project, monkeypatch, ["prune"])

    assert result.exit_code == 0, result.output
    assert not (project / "apm_modules" / "acme" / "pkg-b").exists(), (
        "package removal must still proceed even if hook reconciliation aborts"
    )
    after = {
        p: _read_json(p) if p.exists() else None
        for p in (claude_settings, claude_sidecar, cursor_settings, cursor_sidecar)
    }
    assert after == before, (
        "a failed target resolution must abort before any wipe -- no merged-hook "
        "file or sidecar may be touched"
    )
    assert "Hook reconciliation failed" in result.output


def test_scope_symmetry_is_load_bearing_mutation_break(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation-break proof: neutering the targets= scope re-introduces #2250.

    Simulates the exact pre-fix regression by forcing
    ``HookIntegrator.sync_integration`` to ignore whatever ``targets`` its
    caller passes (mirroring the old code path that always defaulted to
    every ``KNOWN_TARGETS`` entry), then re-runs the narrowing scenario
    from ``test_narrowing_targets_preserves_sibling_hooks_in_dropped_target``
    and proves that assertion would fail against the neutered primitive --
    confirming it is load-bearing, not vacuously true.
    """
    from apm_cli.integration.hook_integrator import HookIntegrator

    project = tmp_path / "proj-mutation-break"
    _install_two_packages_two_targets(project, monkeypatch)
    _narrow_targets(project, ["claude"])

    cursor_sidecar = project / ".cursor" / "apm-hooks.json"
    assert {"pkg-a", "pkg-b"} <= _sidecar_sources(cursor_sidecar)

    original_sync_integration = HookIntegrator.sync_integration

    def _unscoped_sync_integration(self, apm_package, project_root, managed_files=None, **_kw):
        # Drop whatever `targets` the caller supplied -- pre-#2250 behavior.
        return original_sync_integration(
            self, apm_package, project_root, managed_files=managed_files, targets=None
        )

    with patch.object(
        HookIntegrator, "sync_integration", autospec=True, side_effect=_unscoped_sync_integration
    ):
        result = _remove_package("prune", project, monkeypatch, "acme/pkg-b")
    assert result.exit_code == 0, result.output

    assert "pkg-a" not in _sidecar_sources(cursor_sidecar), (
        "with the targets= scope neutered (simulating the pre-#2250 unscoped "
        "wipe), pkg-a's still-installed cursor hook entry must be gone -- "
        "proving the real scoped call is what actually preserves it in "
        "test_narrowing_targets_preserves_sibling_hooks_in_dropped_target"
    )
