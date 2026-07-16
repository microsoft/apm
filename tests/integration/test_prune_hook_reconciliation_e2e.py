"""Hermetic E2E coverage for #2245: prune must reconcile merged hook ownership.

``apm prune`` removes orphaned package directories and their lockfile
``deployed_files``, but historically never touched merged-hook JSON files
(``.claude/settings.json``, ``.cursor/hooks.json``) or their
``apm-hooks.json`` ownership sidecars. This left dead ``_apm_source``
entries behind after a package was dropped from ``apm.yml`` and pruned,
so harnesses like Claude Code kept firing hooks that pointed at deleted
scripts.

``apm uninstall`` already solves this correctly via
``HookIntegrator.sync_integration()`` + a full re-integration pass over
whatever remains declared and installed (see
``_sync_integrations_after_uninstall`` in
``commands/uninstall/engine.py``). These tests drive the REAL ``apm
install`` and ``apm prune`` CLI commands end-to-end (Click ``CliRunner``,
no internals called directly) and stub only the network download seam
(``GitHubPackageDownloader.download_package``), matching the hermetic
pattern already used by
``tests/integration/test_install_content_hash_roundtrip.py`` and
``tests/integration/test_frozen_host_qualified_git_e2e.py``.

Matrix coverage is intentionally bounded to two merge targets (Claude,
Cursor) rather than every harness in ``_MERGE_HOOK_TARGETS`` -- both use
the same schema-strict sidecar-ownership code path, so a third harness
would not exercise new code, only repeat the assertions.
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

# Per-merge-target on-disk layout: config file + its ownership sidecar.
# Both targets are schema-strict (ownership lives only in the sidecar,
# never inline in the native file) -- see hook_integrator._MERGE_HOOK_TARGETS.
_TARGET_LAYOUT = {
    "claude": (".claude/settings.json", ".claude/apm-hooks.json"),
    "cursor": (".cursor/hooks.json", ".cursor/apm-hooks.json"),
}


@pytest.fixture(autouse=True)
def _clear_package_cache() -> None:
    clear_apm_yml_cache()
    yield
    clear_apm_yml_cache()


def _stub_download_package(
    hook_commands: dict[str, str],
    package_deps: dict[str, list[str]] | None = None,
):
    """Build a ``download_package`` stub that materializes a hooked fixture package.

    *hook_commands* maps a dependency's ``repo_url`` (e.g. ``"acme/pkg-a"``)
    to the shell command its lone ``PreToolUse`` hook should run. Packages
    with no entry in the map are still materialized but ship no hooks --
    used to prove prune's *own* package/script removal still works
    unconditionally.

    *package_deps* optionally maps a package's ``repo_url`` to nested
    ``dependencies.apm`` entries so install can resolve a transitive
    chain (see ``test_prune_preserves_transitive_dependency_hooks``).
    """
    nested_deps = package_deps or {}

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
        manifest: dict = {
            "name": pkg_name,
            "version": "1.0.0",
            "description": f"Hermetic prune-reconciliation fixture: {pkg_name}",
        }
        nested = nested_deps.get(dep_ref.repo_url)
        if nested:
            manifest["dependencies"] = {"apm": list(nested)}
        (install_path / "apm.yml").write_text(
            yaml.safe_dump(manifest, sort_keys=False),
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
                "name": "prune-hook-reconciliation-consumer",
                "version": "1.0.0",
                "targets": targets,
                "dependencies": {"apm": dep_repo_urls},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _remove_dependency(project: Path, repo_url: str) -> None:
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
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    hook_commands: dict[str, str],
    *,
    package_deps: dict[str, list[str]] | None = None,
) -> object:
    with patch.object(
        GitHubPackageDownloader,
        "download_package",
        autospec=True,
        side_effect=_stub_download_package(hook_commands, package_deps=package_deps),
    ):
        return _run_cli(project, monkeypatch, ["install", "--no-policy"])


def _run_prune(project: Path, monkeypatch: pytest.MonkeyPatch, *, dry_run: bool = False) -> object:
    args = ["prune", "--dry-run"] if dry_run else ["prune"]
    return _run_cli(project, monkeypatch, args)


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


def _pre_tool_use_commands(settings_path: Path) -> list[str]:
    if not settings_path.exists():
        return []
    data = _read_json(settings_path)
    entries = data.get("hooks", {}).get("PreToolUse", [])
    commands = []
    for entry in entries:
        for handler in entry.get("hooks", []):
            if isinstance(handler, dict) and "command" in handler:
                commands.append(handler["command"])
    return commands


@pytest.mark.parametrize(
    "target",
    [pytest.param("claude", marks=pytest.mark.lifecycle_smoke), "cursor"],
    ids=["claude", "cursor"],
)
def test_prune_removes_merged_hook_entries_and_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    """RED (pre-fix): prune left merged hook entries + sidecar markers behind.

    GREEN (post-fix): pruning an orphaned package's directory and lockfile
    entry also reconciles the merged hook config and its ownership sidecar
    for the target the project declares.
    """
    settings_rel, sidecar_rel = _TARGET_LAYOUT[target]
    project = tmp_path / f"proj-{target}"
    _write_project(project, ["acme/pkg-a"], [target])

    install_result = _run_install(project, monkeypatch, {"acme/pkg-a": "./scripts/pkg-a-hook.sh"})
    assert install_result.exit_code == 0, install_result.output

    package_dir = project / "apm_modules" / "acme" / "pkg-a"
    assert package_dir.is_dir(), "precondition: package installed"
    settings_path = project / settings_rel
    sidecar_path = project / sidecar_rel
    assert "pkg-a" in _sidecar_sources(sidecar_path), (
        f"precondition: pkg-a hook entry merged into {sidecar_rel}"
    )

    _remove_dependency(project, "acme/pkg-a")
    prune_result = _run_prune(project, monkeypatch)
    assert prune_result.exit_code == 0, prune_result.output

    assert not package_dir.exists(), "orphaned package directory must be removed"
    assert "pkg-a" not in _sidecar_sources(sidecar_path), (
        f"pkg-a's merged hook entry must be reconciled out of {sidecar_rel}"
    )
    assert "./scripts/pkg-a-hook.sh" not in _pre_tool_use_commands(settings_path), (
        f"pkg-a's dead hook command must no longer appear in {settings_rel}"
    )


def test_prune_preserves_sibling_hooks_and_manual_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative twin: reconciliation must be scoped, not a blind wipe.

    ``_clean_apm_entries_from_json`` strips every ``_apm_source``-tagged
    entry unconditionally and deletes the whole sidecar. A naive prune ->
    sync_integration() call would therefore also erase the still-declared
    sibling package's hooks and would not distinguish package-owned
    entries from a user's own manual hook -- both would be regressions.
    """
    project = tmp_path / "proj-siblings"
    _write_project(project, ["acme/pkg-a", "acme/pkg-b"], ["claude"])

    install_result = _run_install(
        project,
        monkeypatch,
        {"acme/pkg-a": "./scripts/pkg-a-hook.sh", "acme/pkg-b": "./scripts/pkg-b-hook.sh"},
    )
    assert install_result.exit_code == 0, install_result.output

    settings_path = project / ".claude" / "settings.json"
    sidecar_path = project / ".claude" / "apm-hooks.json"
    assert {"pkg-a", "pkg-b"} <= _sidecar_sources(sidecar_path)

    # Inject a manual, user-owned entry directly into the native file --
    # never in the sidecar, so it carries no _apm_source marker.
    settings_data = _read_json(settings_path)
    settings_data.setdefault("hooks", {}).setdefault("PreToolUse", []).append(
        {
            "matcher": "Bash",
            "hooks": [{"type": "command", "command": "echo manual-user-hook"}],
        }
    )
    settings_path.write_text(json.dumps(settings_data), encoding="utf-8")

    _remove_dependency(project, "acme/pkg-a")
    prune_result = _run_prune(project, monkeypatch)
    assert prune_result.exit_code == 0, prune_result.output

    assert not (project / "apm_modules" / "acme" / "pkg-a").exists()
    assert (project / "apm_modules" / "acme" / "pkg-b").is_dir(), (
        "sibling package declared in apm.yml must survive prune"
    )

    remaining_sources = _sidecar_sources(sidecar_path)
    assert "pkg-a" not in remaining_sources, "pruned package's hook entry must be gone"
    assert "pkg-b" in remaining_sources, (
        "sibling package's hook entry must survive scoped reconciliation"
    )
    commands = _pre_tool_use_commands(settings_path)
    assert "./scripts/pkg-b-hook.sh" in commands, "sibling's hook command must remain"
    assert "echo manual-user-hook" in commands, "manual user-owned hook must remain untouched"
    assert "./scripts/pkg-a-hook.sh" not in commands, "pruned package's hook command must be gone"


def test_prune_preserves_transitive_dependency_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2254: wipe+rebuild must restore hooks owned by a surviving transitive dep.

    Graph: ``keeper`` (direct, no hooks) depends on ``transitive-hooks``
    (transitive, has hooks); ``to-prune`` is a sibling direct orphan.
    Pruning ``to-prune`` triggers ``reconcile_after_removal``. A
    direct-only rebuild would wipe ``transitive-hooks`` entries and never
    put them back even though the package remains installed via ``keeper``.
    """
    project = tmp_path / "proj-transitive-hooks"
    _write_project(project, ["acme/keeper", "acme/to-prune"], ["claude"])

    install_result = _run_install(
        project,
        monkeypatch,
        {
            "acme/transitive-hooks": "./scripts/transitive-hook.sh",
            "acme/to-prune": "./scripts/to-prune-hook.sh",
        },
        package_deps={"acme/keeper": ["acme/transitive-hooks"]},
    )
    assert install_result.exit_code == 0, install_result.output

    transitive_dir = project / "apm_modules" / "acme" / "transitive-hooks"
    assert transitive_dir.is_dir(), "precondition: transitive package installed"
    settings_path = project / ".claude" / "settings.json"
    sidecar_path = project / ".claude" / "apm-hooks.json"
    assert "transitive-hooks" in _sidecar_sources(sidecar_path), (
        "precondition: transitive package's hooks merged"
    )
    assert "./scripts/transitive-hook.sh" in _pre_tool_use_commands(settings_path)

    _remove_dependency(project, "acme/to-prune")
    prune_result = _run_prune(project, monkeypatch)
    assert prune_result.exit_code == 0, prune_result.output

    assert not (project / "apm_modules" / "acme" / "to-prune").exists()
    assert transitive_dir.is_dir(), "transitive package must survive via keeper"
    assert "to-prune" not in _sidecar_sources(sidecar_path)
    assert "transitive-hooks" in _sidecar_sources(sidecar_path), (
        "transitive dependency's hook ownership must be rebuilt after prune wipe"
    )
    commands = _pre_tool_use_commands(settings_path)
    assert "./scripts/transitive-hook.sh" in commands, (
        "transitive dependency's hook command must survive reconcile_after_removal"
    )
    assert "./scripts/to-prune-hook.sh" not in commands


def test_prune_hook_reconciliation_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running prune twice in a row must not error or change state further."""
    project = tmp_path / "proj-idempotent"
    _write_project(project, ["acme/pkg-a"], ["claude"])
    install_result = _run_install(project, monkeypatch, {"acme/pkg-a": "./scripts/pkg-a-hook.sh"})
    assert install_result.exit_code == 0, install_result.output

    _remove_dependency(project, "acme/pkg-a")
    first = _run_prune(project, monkeypatch)
    assert first.exit_code == 0, first.output

    settings_path = project / ".claude" / "settings.json"
    sidecar_path = project / ".claude" / "apm-hooks.json"
    state_after_first = (
        _read_json(settings_path) if settings_path.exists() else None,
        sidecar_path.exists(),
    )

    second = _run_prune(project, monkeypatch)
    assert second.exit_code == 0, second.output
    state_after_second = (
        _read_json(settings_path) if settings_path.exists() else None,
        sidecar_path.exists(),
    )
    assert state_after_second == state_after_first, "second prune run must be a no-op"


def test_prune_dry_run_does_not_touch_hook_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--dry-run`` must leave merged hook config and sidecar untouched."""
    project = tmp_path / "proj-dry-run"
    _write_project(project, ["acme/pkg-a"], ["claude"])
    install_result = _run_install(project, monkeypatch, {"acme/pkg-a": "./scripts/pkg-a-hook.sh"})
    assert install_result.exit_code == 0, install_result.output

    settings_path = project / ".claude" / "settings.json"
    sidecar_path = project / ".claude" / "apm-hooks.json"
    before_settings = _read_json(settings_path)
    before_sidecar = _read_json(sidecar_path)

    _remove_dependency(project, "acme/pkg-a")
    dry_run_result = _run_prune(project, monkeypatch, dry_run=True)
    assert dry_run_result.exit_code == 0, dry_run_result.output

    assert (project / "apm_modules" / "acme" / "pkg-a").is_dir(), (
        "dry-run must not remove the package directory"
    )
    assert _read_json(settings_path) == before_settings, "dry-run must not touch merged config"
    assert _read_json(sidecar_path) == before_sidecar, "dry-run must not touch the sidecar"


def test_prune_hook_reconciliation_failure_does_not_abort_package_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Partial-failure semantics: a reconcile error must not roll back removal.

    This mirrors prune's existing per-step error tolerance (a failed
    ``safe_rmtree`` or lockfile write is logged, not fatal) -- a hook
    reconciliation failure must behave the same way, not crash the whole
    command or leave the package directory in place.
    """
    project = tmp_path / "proj-partial-failure"
    _write_project(project, ["acme/pkg-a"], ["claude"])
    install_result = _run_install(project, monkeypatch, {"acme/pkg-a": "./scripts/pkg-a-hook.sh"})
    assert install_result.exit_code == 0, install_result.output

    _remove_dependency(project, "acme/pkg-a")
    with patch(
        "apm_cli.integration.hook_integrator.HookIntegrator.reconcile_after_removal",
        side_effect=RuntimeError("simulated reconciliation failure"),
    ):
        prune_result = _run_prune(project, monkeypatch)

    assert prune_result.exit_code == 0, prune_result.output
    assert not (project / "apm_modules" / "acme" / "pkg-a").exists(), (
        "package removal must not be rolled back by a reconciliation failure"
    )
    assert "Hook reconciliation failed" in prune_result.output, (
        "a failed reconciliation must surface a user-visible diagnostic, not "
        "fail silently -- users need to know hook entries may be stale"
    )


def test_prune_orchestration_call_is_load_bearing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation-break proof: neutering prune's call to the reconcile owner
    must re-introduce the exact #2245 symptom (dead merged hook entry).

    This does not edit source; it simulates the mutation an accidental
    revert of the orchestration call would cause (a no-op reconcile) and
    proves ``test_prune_removes_merged_hook_entries_and_sidecar`` would
    fail against it -- confirming that assertion is load-bearing rather
    than vacuously true.
    """
    project = tmp_path / "proj-mutation-break"
    _write_project(project, ["acme/pkg-a"], ["claude"])
    install_result = _run_install(project, monkeypatch, {"acme/pkg-a": "./scripts/pkg-a-hook.sh"})
    assert install_result.exit_code == 0, install_result.output

    sidecar_path = project / ".claude" / "apm-hooks.json"
    assert "pkg-a" in _sidecar_sources(sidecar_path)

    _remove_dependency(project, "acme/pkg-a")
    with patch(
        "apm_cli.integration.hook_integrator.HookIntegrator.reconcile_after_removal",
        return_value={"files_removed": 0, "errors": 0},
    ) as neutered_reconcile:
        prune_result = _run_prune(project, monkeypatch)
        assert neutered_reconcile.called, (
            "prune must still call the reconcile owner even when neutered"
        )
    assert prune_result.exit_code == 0, prune_result.output
    assert "pkg-a" in _sidecar_sources(sidecar_path), (
        "with the reconcile call neutered (simulating its removal), the "
        "stale hook entry must still be present -- proving the real call "
        "is what actually cleans it in "
        "test_prune_removes_merged_hook_entries_and_sidecar"
    )
