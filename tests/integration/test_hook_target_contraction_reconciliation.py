"""Integration contracts for #2253: target-contraction merge-hook cleanup.

Narrowing a project's declared ``targets:`` list (e.g. ``[claude, codex]``
-> ``[claude]``) never cleaned up the dropped target's APM-owned
merge-hook JSON config (``.codex/hooks.json``) or its ``apm-hooks.json``
ownership sidecar, even after a subsequent ``apm install`` + ``apm prune``.

Root cause: merge-hook config files are deliberately excluded from
``deployed_files`` tracking (see ``HookIntegrator``'s "don't track the
config file in target_paths -- it's a shared file"), so the generic
file-based reconciliation in ``manifest_reconcile.py`` never sees them.
Separately, ``HookIntegrator.reconcile_after_removal`` (used by both
``apm prune`` and ``apm uninstall``) intentionally scopes its wipe to the
SAME resolved target set the rebuild loop uses (#2250/#2252) -- correct
for that bug, but it permanently walls prune/uninstall off from a target
DROPPED from ``targets:`` entirely.

The fix extends two existing owners rather than inventing a third:
``manifest_reconcile.reconcile_dropped_merge_hook_targets`` (computes
which target names dropped, mirroring ``reconcile_deployed_state``'s own
"allowed = active union declared" semantics) and
``HookIntegrator.reconcile_dropped_targets`` (the sole owner of merge-hook
JSON/sidecar mutation for the dropped names). Wired at the top of
``LockfileBuilder.build_and_save()`` (install path) and at the end of
``reconcile_deployed_state()`` (shared by ``apm compile``/``apm update``).

These tests drive the REAL ``apm install``/``apm prune``/``apm
compile``/``apm uninstall`` CLI commands end-to-end (Click ``CliRunner``),
stubbing only the network download seam
(``GitHubPackageDownloader.download_package``) -- integration contracts,
not real-Git E2E (that stronger fidelity class belongs to the strict,
unchanged PR #2266 local scenario, which remains this fix's
highest-fidelity confirmation post-merge).
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

# Both targets are schema-strict (ownership lives only in the sidecar,
# never inline in native JSON) -- see hook_integrator._MERGE_HOOK_TARGETS.
_TARGET_LAYOUT = {
    "claude": (".claude/settings.json", ".claude/apm-hooks.json"),
    "codex": (".codex/hooks.json", ".codex/apm-hooks.json"),
    "cursor": (".cursor/hooks.json", ".cursor/apm-hooks.json"),
}

_MARKER = "fixture-claude-codex-hook-narrow"


@pytest.fixture(autouse=True)
def _clear_package_cache() -> None:
    clear_apm_yml_cache()
    yield
    clear_apm_yml_cache()


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
                    "description": f"Hermetic target-contraction fixture: {pkg_name}",
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


def _write_project(project: Path, dep_repo_urls: list[str], targets: list[str] | None) -> None:
    project.mkdir(parents=True, exist_ok=True)
    manifest: dict = {
        "name": "hook-target-contraction-consumer",
        "version": "1.0.0",
        "dependencies": {"apm": dep_repo_urls},
    }
    if targets is not None:
        manifest["targets"] = targets
    (project / "apm.yml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    clear_apm_yml_cache()


def _narrow_targets(project: Path, targets: list[str]) -> None:
    manifest_path = project / "apm.yml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["targets"] = targets
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    clear_apm_yml_cache()


def _remove_dependency(project: Path, repo_url: str) -> None:
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
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    hook_commands: dict[str, str],
    *,
    extra_args: list[str] | None = None,
) -> object:
    with patch.object(
        GitHubPackageDownloader,
        "download_package",
        autospec=True,
        side_effect=_stub_download_package(hook_commands),
    ):
        return _run_cli(project, monkeypatch, ["install", "--no-policy", *(extra_args or [])])


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _sidecar_sources(sidecar_path: Path) -> set[str]:
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


def _install_claude_codex_pkg(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_project(project, ["acme/pkg-a"], ["claude", "codex"])
    result = _run_install(project, monkeypatch, {"acme/pkg-a": "./scripts/pkg-a-hook.sh"})
    assert result.exit_code == 0, result.output


def test_narrow_then_install_removes_dropped_target_hook_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED (pre-fix): narrowing targets: [claude, codex] -> [claude] then
    running `apm install` + `apm prune` left .codex/hooks.json and its
    apm-hooks.json ownership sidecar fully intact with pkg-a's marker.

    GREEN (post-fix): the dropped target's merge-hook state is reconciled
    during the narrowing `apm install` itself.
    """
    project = tmp_path / "proj-main"
    _install_claude_codex_pkg(project, monkeypatch)

    codex_settings = project / ".codex" / "hooks.json"
    codex_sidecar = project / ".codex" / "apm-hooks.json"
    assert "pkg-a" in _sidecar_sources(codex_sidecar), "precondition: codex hook merged"

    _narrow_targets(project, ["claude"])
    install_result = _run_install(project, monkeypatch, {"acme/pkg-a": "./scripts/pkg-a-hook.sh"})
    assert install_result.exit_code == 0, install_result.output

    prune_result = _run_cli(project, monkeypatch, ["prune"])
    assert prune_result.exit_code == 0, prune_result.output

    assert "pkg-a" not in _sidecar_sources(codex_sidecar), (
        "dropped target's hook entry must be reconciled after narrow+install"
    )
    assert not codex_sidecar.exists(), (
        "dropped target's ownership sidecar must be removed once empty"
    )
    assert "./scripts/pkg-a-hook.sh" not in _pre_tool_use_commands(codex_settings), (
        "dropped target's dead hook command must not survive"
    )


def test_narrow_then_install_preserves_retained_target_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative twin: the still-declared target's hooks must survive intact."""
    project = tmp_path / "proj-retained"
    _install_claude_codex_pkg(project, monkeypatch)

    claude_settings = project / ".claude" / "settings.json"
    claude_sidecar = project / ".claude" / "apm-hooks.json"

    _narrow_targets(project, ["claude"])
    result = _run_install(project, monkeypatch, {"acme/pkg-a": "./scripts/pkg-a-hook.sh"})
    assert result.exit_code == 0, result.output

    assert "pkg-a" in _sidecar_sources(claude_sidecar), (
        "retained target's hook entry must survive the narrowing install"
    )
    assert "./scripts/pkg-a-hook.sh" in _pre_tool_use_commands(claude_settings)


def test_narrow_then_install_preserves_user_owned_codex_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative twin: a hand-authored, unowned codex entry (no _apm_source)
    must survive the dropped-target cleanup untouched."""
    project = tmp_path / "proj-user-owned"
    _install_claude_codex_pkg(project, monkeypatch)

    codex_settings = project / ".codex" / "hooks.json"
    data = _read_json(codex_settings)
    data.setdefault("hooks", {}).setdefault("PreToolUse", []).append(
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo manual-codex-hook"}]}
    )
    codex_settings.write_text(json.dumps(data), encoding="utf-8")

    _narrow_targets(project, ["claude"])
    result = _run_install(project, monkeypatch, {"acme/pkg-a": "./scripts/pkg-a-hook.sh"})
    assert result.exit_code == 0, result.output

    assert "echo manual-codex-hook" in _pre_tool_use_commands(codex_settings), (
        "user-owned codex entry must survive dropped-target reconciliation"
    )
    assert "pkg-a" not in _sidecar_sources(project / ".codex" / "apm-hooks.json")


def test_no_target_change_install_is_a_noop_for_hook_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control: reinstalling with the SAME declared targets must
    not touch either target's merge-hook state at all."""
    project = tmp_path / "proj-no-change"
    _install_claude_codex_pkg(project, monkeypatch)

    codex_settings = project / ".codex" / "hooks.json"
    codex_sidecar = project / ".codex" / "apm-hooks.json"
    before = (_read_json(codex_settings), _read_json(codex_sidecar))

    result = _run_install(project, monkeypatch, {"acme/pkg-a": "./scripts/pkg-a-hook.sh"})
    assert result.exit_code == 0, result.output

    after = (_read_json(codex_settings), _read_json(codex_sidecar))
    assert after == before, "unchanged targets: must leave codex hook state untouched"


def test_narrow_then_install_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "proj-idempotent"
    _install_claude_codex_pkg(project, monkeypatch)
    _narrow_targets(project, ["claude"])

    first = _run_install(project, monkeypatch, {"acme/pkg-a": "./scripts/pkg-a-hook.sh"})
    assert first.exit_code == 0, first.output

    codex_sidecar = project / ".codex" / "apm-hooks.json"
    codex_settings = project / ".codex" / "hooks.json"
    state_after_first = (
        codex_sidecar.exists(),
        _read_json(codex_settings) if codex_settings.exists() else None,
    )

    second = _run_install(project, monkeypatch, {"acme/pkg-a": "./scripts/pkg-a-hook.sh"})
    assert second.exit_code == 0, second.output

    state_after_second = (
        codex_sidecar.exists(),
        _read_json(codex_settings) if codex_settings.exists() else None,
    )
    assert state_after_second == state_after_first, "second narrowed install must be a no-op"


def test_final_uninstall_after_narrow_still_removes_remaining_target_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Composition: after a narrow+install already cleaned the dropped
    target, a subsequent `apm uninstall` for the last package must still
    clean the RETAINED target's state via the existing, unchanged
    prune/uninstall path (#2250/#2252) -- this fix must not regress it."""
    project = tmp_path / "proj-final-uninstall"
    _install_claude_codex_pkg(project, monkeypatch)
    _narrow_targets(project, ["claude"])
    install_result = _run_install(project, monkeypatch, {"acme/pkg-a": "./scripts/pkg-a-hook.sh"})
    assert install_result.exit_code == 0, install_result.output

    claude_sidecar = project / ".claude" / "apm-hooks.json"
    assert "pkg-a" in _sidecar_sources(claude_sidecar), "precondition: claude still owns pkg-a"

    uninstall_result = _run_cli(project, monkeypatch, ["uninstall", "acme/pkg-a"])
    assert uninstall_result.exit_code == 0, uninstall_result.output

    assert "pkg-a" not in _sidecar_sources(claude_sidecar), (
        "final uninstall must still clean the retained target's hook state"
    )


def test_prune_alone_still_does_not_clean_dropped_target_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit #2250/#2252 negative control: prune (without a preceding
    `apm install`) must NOT itself clean a dropped target's state -- that
    stays exclusively the install/compile/update-lifecycle owner's job,
    per this fix's canonical-owner boundary."""
    project = tmp_path / "proj-prune-negative"
    _install_claude_codex_pkg(project, monkeypatch)

    codex_sidecar = project / ".codex" / "apm-hooks.json"
    assert "pkg-a" in _sidecar_sources(codex_sidecar)

    _narrow_targets(project, ["claude"])
    prune_result = _run_cli(project, monkeypatch, ["prune"])
    assert prune_result.exit_code == 0, prune_result.output

    assert "pkg-a" in _sidecar_sources(codex_sidecar), (
        "prune alone (no install) must not clean dropped-target hook state"
    )


def test_widen_then_narrow_removes_dropped_cursor_hook_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Generic-owner proof, Cursor variant (required-gate node-2 evidence):
    Claude -> Claude+Cursor -> Claude (widen then narrow back, not just a
    single narrow from an initial multi-target install) previously left
    `.cursor/hooks.json` and its `apm-hooks.json` ownership sidecar fully
    intact with the dropped target's marker, even when Cursor's own
    file-based deployed-file reconciliation (e.g. rules content) succeeds
    -- because merge-hook JSON is deliberately excluded from
    `deployed_files` tracking for every merge-hook target, not just Codex.

    GREEN (post-fix): `HookIntegrator.reconcile_dropped_targets` is generic
    over `_MERGE_HOOK_TARGETS` (Claude, Cursor, Codex, Gemini, Windsurf,
    Antigravity) -- narrowing back to Claude-only reconciles Cursor's
    merge-hook state exactly like Codex's, asserted directly against the
    native merged config and the sidecar here rather than relying on any
    audit/orphan-detection surface (which only reports the separate,
    out-of-scope `.agents/skills` target-row defect)."""
    project = tmp_path / "proj-cursor-widen-narrow"
    _write_project(project, ["acme/pkg-a"], ["claude"])
    result = _run_install(project, monkeypatch, {"acme/pkg-a": "./scripts/pkg-a-hook.sh"})
    assert result.exit_code == 0, result.output

    _narrow_targets(project, ["claude", "cursor"])  # widen
    widen_result = _run_install(project, monkeypatch, {"acme/pkg-a": "./scripts/pkg-a-hook.sh"})
    assert widen_result.exit_code == 0, widen_result.output

    cursor_settings = project / ".cursor" / "hooks.json"
    cursor_sidecar = project / ".cursor" / "apm-hooks.json"
    assert "pkg-a" in _sidecar_sources(cursor_sidecar), (
        "precondition: cursor hook merged after widen"
    )
    assert "./scripts/pkg-a-hook.sh" in _pre_tool_use_commands(cursor_settings), (
        "precondition: cursor native config carries the widened install's hook command"
    )

    _narrow_targets(project, ["claude"])  # narrow back
    narrow_result = _run_install(project, monkeypatch, {"acme/pkg-a": "./scripts/pkg-a-hook.sh"})
    assert narrow_result.exit_code == 0, narrow_result.output

    prune_result = _run_cli(project, monkeypatch, ["prune"])
    assert prune_result.exit_code == 0, prune_result.output

    assert "pkg-a" not in _sidecar_sources(cursor_sidecar), (
        "dropped cursor target's hook entry must be reconciled after narrow+install, "
        "asserted directly against the sidecar, not via audit"
    )
    assert not cursor_sidecar.exists(), (
        "dropped cursor target's ownership sidecar must be removed once empty"
    )
    assert "./scripts/pkg-a-hook.sh" not in _pre_tool_use_commands(cursor_settings), (
        "dropped cursor target's dead hook command must not survive in the native "
        "merged config, asserted directly, not via audit"
    )


def test_widen_then_narrow_preserves_user_owned_cursor_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative twin (Cursor variant): a hand-authored, unowned cursor entry
    (no `_apm_source`) must survive the dropped-target cleanup untouched,
    proving the generic owner's marker-based ownership check -- not a
    per-target special case -- gates the deletion."""
    project = tmp_path / "proj-cursor-user-owned"
    _write_project(project, ["acme/pkg-a"], ["claude"])
    result = _run_install(project, monkeypatch, {"acme/pkg-a": "./scripts/pkg-a-hook.sh"})
    assert result.exit_code == 0, result.output

    _narrow_targets(project, ["claude", "cursor"])  # widen
    widen_result = _run_install(project, monkeypatch, {"acme/pkg-a": "./scripts/pkg-a-hook.sh"})
    assert widen_result.exit_code == 0, widen_result.output

    cursor_settings = project / ".cursor" / "hooks.json"
    data = _read_json(cursor_settings)
    data.setdefault("hooks", {}).setdefault("PreToolUse", []).append(
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo manual-cursor-hook"}]}
    )
    cursor_settings.write_text(json.dumps(data), encoding="utf-8")

    _narrow_targets(project, ["claude"])  # narrow back
    narrow_result = _run_install(project, monkeypatch, {"acme/pkg-a": "./scripts/pkg-a-hook.sh"})
    assert narrow_result.exit_code == 0, narrow_result.output

    assert "echo manual-cursor-hook" in _pre_tool_use_commands(cursor_settings), (
        "user-owned cursor entry must survive dropped-target reconciliation"
    )
    assert "pkg-a" not in _sidecar_sources(project / ".cursor" / "apm-hooks.json")


def test_dry_run_install_does_not_reconcile_dropped_target_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`apm install --dry-run` must never mutate dropped-target hook state --
    it never reaches `LockfileBuilder.build_and_save()` at all."""
    project = tmp_path / "proj-dry-run"
    _install_claude_codex_pkg(project, monkeypatch)

    codex_sidecar = project / ".codex" / "apm-hooks.json"
    assert "pkg-a" in _sidecar_sources(codex_sidecar)

    _narrow_targets(project, ["claude"])
    result = _run_install(
        project,
        monkeypatch,
        {"acme/pkg-a": "./scripts/pkg-a-hook.sh"},
        extra_args=["--dry-run"],
    )
    assert result.exit_code == 0, result.output

    assert "pkg-a" in _sidecar_sources(codex_sidecar), (
        "--dry-run must never reconcile dropped-target hook state"
    )


def test_compile_reconciles_dropped_target_hook_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parity proof: `apm compile` shares `reconcile_deployed_state` with
    `apm install`'s update-lifecycle path, so it also reconciles a dropped
    target's hook state even when the user runs compile without an
    intervening explicit `apm install`."""
    project = tmp_path / "proj-compile"
    _install_claude_codex_pkg(project, monkeypatch)

    codex_sidecar = project / ".codex" / "apm-hooks.json"
    assert "pkg-a" in _sidecar_sources(codex_sidecar)

    _narrow_targets(project, ["claude"])
    result = _run_cli(project, monkeypatch, ["compile", "--target", "claude"])
    assert result.exit_code == 0, result.output

    assert "pkg-a" not in _sidecar_sources(codex_sidecar), (
        "apm compile must reconcile dropped-target hook state via the shared "
        "reconcile_deployed_state path"
    )


def test_narrow_and_remove_last_dependency_in_one_install_still_cleans_hook_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lifecycle-timing regression: narrowing targets: AND removing the
    package's only remaining manifest entry in the SAME `apm install`
    invocation must still clean the dropped target's hook state --
    exercising the top-of-`build_and_save` call site even when
    `installed_packages` ends up empty (the shape that would reach the
    early-return before ever running `_attach_deployed_files`)."""
    project = tmp_path / "proj-narrow-and-remove"
    _install_claude_codex_pkg(project, monkeypatch)

    codex_sidecar = project / ".codex" / "apm-hooks.json"
    assert "pkg-a" in _sidecar_sources(codex_sidecar)

    manifest_path = project / "apm.yml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["targets"] = ["claude"]
    manifest["dependencies"]["apm"] = []
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    clear_apm_yml_cache()

    result = _run_install(project, monkeypatch, {})
    assert result.exit_code == 0, result.output

    assert "pkg-a" not in _sidecar_sources(codex_sidecar), (
        "hook-target reconciliation must fire even when installed_packages ends up empty"
    )


def test_explicit_transient_target_does_not_clean_other_declared_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Union-rule negative twin: a one-off `--target claude` override must
    NOT treat codex as dropped while apm.yml still declares it."""
    project = tmp_path / "proj-transient-target"
    _install_claude_codex_pkg(project, monkeypatch)

    codex_sidecar = project / ".codex" / "apm-hooks.json"
    assert "pkg-a" in _sidecar_sources(codex_sidecar)

    result = _run_install(
        project,
        monkeypatch,
        {"acme/pkg-a": "./scripts/pkg-a-hook.sh"},
        extra_args=["--target", "claude"],
    )
    assert result.exit_code == 0, result.output

    assert "pkg-a" in _sidecar_sources(codex_sidecar), (
        "a transient --target override must not clean a still-declared sibling target"
    )


def test_declared_targets_absent_is_a_noop_preserve_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2059 symmetry: a project with no `targets:` field at all (auto-detect
    / --target-only consumer) has no declared universe to check
    dropped-ness against -- any pre-existing merge-hook state for a target
    not currently active must be preserved untouched, exactly like the
    legacy `union_preserving` preserve-all fallback."""
    project = tmp_path / "proj-no-declared-targets"
    _write_project(project, ["acme/pkg-a"], targets=None)

    # Simulate pre-existing codex hook state that predates any declared
    # targets: field (hand-authored, or left over from a differently
    # configured environment) -- with declared_targets=None there is no
    # universe to check dropped-ness against, so this must never be touched.
    codex_dir = project / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "hooks.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []}]}}),
        encoding="utf-8",
    )
    (codex_dir / "apm-hooks.json").write_text(
        json.dumps({"PreToolUse": [{"matcher": "Bash", "hooks": [], "_apm_source": _MARKER}]}),
        encoding="utf-8",
    )

    result = _run_install(
        project,
        monkeypatch,
        {"acme/pkg-a": "./scripts/pkg-a-hook.sh"},
        extra_args=["--target", "claude"],
    )
    assert result.exit_code == 0, result.output

    assert (codex_dir / "apm-hooks.json").exists(), (
        "declared_targets=None must be a hard no-op -- pre-existing codex "
        "sidecar must survive untouched"
    )
    assert _MARKER in _sidecar_sources(codex_dir / "apm-hooks.json")
