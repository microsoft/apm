"""Unit coverage for #2254: surviving deps for clear+rebuild include transitive."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from apm_cli.commands.uninstall.engine import _sync_integrations_after_uninstall
from apm_cli.deps.lockfile import LockedDependency, LockFile, get_lockfile_path
from apm_cli.integration.hook_integrator import HookIntegrator
from apm_cli.models.apm_package import (
    APMPackage,
    GitReferenceType,
    PackageInfo,
    ResolvedReference,
    clear_apm_yml_cache,
    surviving_dependency_refs_for_reintegration,
)
from apm_cli.models.dependency.reference import DependencyReference


@pytest.fixture(autouse=True)
def _clear_package_cache() -> None:
    clear_apm_yml_cache()
    yield
    clear_apm_yml_cache()


def _write_hooked_package(modules: Path, owner: str, name: str, command: str) -> Path:
    pkg_path = modules / owner / name
    pkg_path.mkdir(parents=True, exist_ok=True)
    (pkg_path / "apm.yml").write_text(
        yaml.safe_dump({"name": name, "version": "1.0.0"}, sort_keys=False),
        encoding="utf-8",
    )
    hooks_dir = pkg_path / ".apm" / "hooks"
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
    return pkg_path


def _package_info(pkg_path: Path, repo_url: str) -> PackageInfo:
    dep_ref = DependencyReference.parse(repo_url)
    return PackageInfo(
        package=APMPackage.from_apm_yml(pkg_path / "apm.yml"),
        install_path=pkg_path,
        dependency_ref=dep_ref,
        installed_at=datetime.now().isoformat(),
        resolved_reference=ResolvedReference(
            original_ref="main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit="abc123",
            ref_name="main",
        ),
    )


def test_surviving_refs_include_lockfile_transitive(tmp_path: Path) -> None:
    """Lockfile package deps (depth>1) must appear in the survivor list."""
    (tmp_path / "apm.yml").write_text(
        "name: root\nversion: 0.0.0\ndependencies:\n  apm:\n    - acme/keeper\n",
        encoding="utf-8",
    )
    lockfile = LockFile()
    lockfile.add_dependency(LockedDependency(repo_url="acme/keeper", depth=1))
    lockfile.add_dependency(
        LockedDependency(repo_url="acme/transitive", depth=2, resolved_by="acme/keeper")
    )
    lockfile.write(get_lockfile_path(tmp_path))

    apm_package = APMPackage.from_apm_yml(tmp_path / "apm.yml")
    refs = surviving_dependency_refs_for_reintegration(apm_package, tmp_path)
    urls = {ref.repo_url for ref in refs}
    assert urls == {"acme/keeper", "acme/transitive"}


def test_surviving_refs_fallback_without_lockfile(tmp_path: Path) -> None:
    """No lockfile: fall back to manifest directs (prod + dev)."""
    (tmp_path / "apm.yml").write_text(
        "name: root\nversion: 0.0.0\n"
        "dependencies:\n  apm:\n    - acme/prod\n"
        "devDependencies:\n  apm:\n    - acme/dev\n",
        encoding="utf-8",
    )
    apm_package = APMPackage.from_apm_yml(tmp_path / "apm.yml")
    refs = surviving_dependency_refs_for_reintegration(apm_package, tmp_path)
    urls = {ref.repo_url for ref in refs}
    assert urls == {"acme/prod", "acme/dev"}


def test_surviving_refs_prefer_passed_in_memory_lockfile(tmp_path: Path) -> None:
    """Uninstall must pass the mutated in-memory lockfile; disk may still be stale."""
    (tmp_path / "apm.yml").write_text(
        "name: root\nversion: 0.0.0\ndependencies:\n  apm:\n    - acme/keeper\n",
        encoding="utf-8",
    )
    stale = LockFile()
    stale.add_dependency(LockedDependency(repo_url="acme/keeper", depth=1))
    stale.add_dependency(LockedDependency(repo_url="acme/removed", depth=1))
    stale.write(get_lockfile_path(tmp_path))

    survivors = LockFile()
    survivors.add_dependency(LockedDependency(repo_url="acme/keeper", depth=1))
    survivors.add_dependency(
        LockedDependency(repo_url="acme/transitive", depth=2, resolved_by="acme/keeper")
    )

    apm_package = APMPackage.from_apm_yml(tmp_path / "apm.yml")
    refs = surviving_dependency_refs_for_reintegration(apm_package, tmp_path, lockfile=survivors)
    urls = {ref.repo_url for ref in refs}
    assert urls == {"acme/keeper", "acme/transitive"}
    assert "acme/removed" not in urls


def test_reconcile_after_removal_rebuilds_transitive_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """reconcile_after_removal must re-integrate hooks from depth>1 survivors."""
    monkeypatch.chdir(tmp_path)
    modules = tmp_path / "apm_modules"
    keeper_path = modules / "acme" / "keeper"
    keeper_path.mkdir(parents=True, exist_ok=True)
    (keeper_path / "apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": "keeper",
                "version": "1.0.0",
                "dependencies": {"apm": ["acme/transitive-hooks"]},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    transitive_path = _write_hooked_package(
        modules, "acme", "transitive-hooks", "./scripts/transitive-hook.sh"
    )

    (tmp_path / "apm.yml").write_text(
        "name: root\nversion: 0.0.0\ntargets:\n  - claude\n"
        "dependencies:\n  apm:\n    - acme/keeper\n",
        encoding="utf-8",
    )
    lockfile = LockFile()
    lockfile.add_dependency(LockedDependency(repo_url="acme/keeper", depth=1))
    lockfile.add_dependency(
        LockedDependency(
            repo_url="acme/transitive-hooks",
            depth=2,
            resolved_by="acme/keeper",
        )
    )
    lockfile.write(get_lockfile_path(tmp_path))

    # Seed merged hooks from the transitive package, then wipe via reconcile.
    integrator = HookIntegrator()
    pkg_info = _package_info(transitive_path, "acme/transitive-hooks")
    from apm_cli.integration.targets import KNOWN_TARGETS

    integrator.integrate_hooks_for_target(KNOWN_TARGETS["claude"], pkg_info, tmp_path)

    sidecar = tmp_path / ".claude" / "apm-hooks.json"
    settings = tmp_path / ".claude" / "settings.json"
    assert sidecar.exists()

    apm_package = APMPackage.from_apm_yml(tmp_path / "apm.yml")
    stats = integrator.reconcile_after_removal(apm_package, tmp_path)
    assert stats.get("errors", 0) == 0

    sidecar_data = json.loads(sidecar.read_text(encoding="utf-8"))
    sources: set[str] = set()
    for entries in sidecar_data.values():
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict) and entry.get("_apm_source"):
                    sources.add(entry["_apm_source"])
    assert "transitive-hooks" in sources

    settings_data = json.loads(settings.read_text(encoding="utf-8"))
    commands = []
    for entry in settings_data.get("hooks", {}).get("PreToolUse", []):
        for handler in entry.get("hooks", []):
            if isinstance(handler, dict) and "command" in handler:
                commands.append(handler["command"])
    assert "./scripts/transitive-hook.sh" in commands


def test_uninstall_phase2_reintegrates_transitive_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Uninstall Phase 2 must walk lockfile survivors, not prod directs only."""
    monkeypatch.chdir(tmp_path)
    modules = tmp_path / "apm_modules"
    keeper_path = modules / "acme" / "keeper"
    keeper_path.mkdir(parents=True, exist_ok=True)
    (keeper_path / "apm.yml").write_text(
        yaml.safe_dump({"name": "keeper", "version": "1.0.0"}, sort_keys=False),
        encoding="utf-8",
    )
    transitive_path = _write_hooked_package(
        modules, "acme", "transitive-hooks", "./scripts/transitive-hook.sh"
    )

    (tmp_path / "apm.yml").write_text(
        "name: root\nversion: 0.0.0\ntargets:\n  - claude\n"
        "dependencies:\n  apm:\n    - acme/keeper\n",
        encoding="utf-8",
    )

    # Seed hooks, then wipe them the same way Phase 1 does.
    integrator = HookIntegrator()
    pkg_info = _package_info(transitive_path, "acme/transitive-hooks")
    from apm_cli.integration.targets import KNOWN_TARGETS

    integrator.integrate_hooks_for_target(KNOWN_TARGETS["claude"], pkg_info, tmp_path)
    apm_package = APMPackage.from_apm_yml(tmp_path / "apm.yml")
    integrator.sync_integration(apm_package, tmp_path, managed_files=set())

    survivors = LockFile()
    survivors.add_dependency(LockedDependency(repo_url="acme/keeper", depth=1))
    survivors.add_dependency(
        LockedDependency(
            repo_url="acme/transitive-hooks",
            depth=2,
            resolved_by="acme/keeper",
        )
    )

    logger = MagicMock()
    _sync_integrations_after_uninstall(
        apm_package,
        tmp_path,
        set(),
        logger,
        lockfile=survivors,
    )

    sidecar = tmp_path / ".claude" / "apm-hooks.json"
    assert sidecar.exists()
    sidecar_data = json.loads(sidecar.read_text(encoding="utf-8"))
    sources: set[str] = set()
    for entries in sidecar_data.values():
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict) and entry.get("_apm_source"):
                    sources.add(entry["_apm_source"])
    assert "transitive-hooks" in sources, (
        "uninstall Phase 2 must rebuild hooks from transitive lockfile survivors"
    )
