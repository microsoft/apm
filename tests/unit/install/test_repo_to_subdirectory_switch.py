"""#2926: switching a whole-repository dependency to subdirectory dependencies.

Switching ``owner/repo`` to ``path:`` dependencies of the same repository
used to leave the old checkout as the parent of the new installs. Its other
skills were then flagged as orphaned by ``apm deps list`` and ``apm prune``
while ``apm deps tree`` showed a clean graph.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.commands._helpers import _build_expected_install_paths, _find_orphaned_packages
from apm_cli.commands.deps import cli as deps_cli
from apm_cli.commands.deps._utils import _scan_installed_packages
from apm_cli.commands.deps.cli import _resolve_scope_deps
from apm_cli.core.scope import InstallScope
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.phases import cleanup
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import APMPackage, clear_apm_yml_cache
from apm_cli.utils.diagnostics import DiagnosticCollector

pytestmark = pytest.mark.component

REPO = "owner/repo"
SUBDIRS = ("skills/a", "skills/b")
FRESH_SUBDIR_TREE = {
    "owner/repo/skills/a/.apm-pin",
    "owner/repo/skills/a/SKILL.md",
    "owner/repo/skills/a/agents/openai.yaml",
    "owner/repo/skills/b/.apm-pin",
    "owner/repo/skills/b/SKILL.md",
}


def _whole_repo_dep() -> LockedDependency:
    return LockedDependency(repo_url=REPO, host="github.com", package_type="skill_bundle")


def _subdir_dep(path: str) -> LockedDependency:
    return LockedDependency(
        repo_url=REPO,
        host="github.com",
        virtual_path=path,
        is_virtual=True,
        package_type="claude_skill",
    )


def _lockfile(*deps: LockedDependency) -> LockFile:
    lock = LockFile()
    for dep in deps:
        lock.add_dependency(dep)
    return lock


def _write(path: Path, text: str = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_whole_repo_checkout(apm_modules: Path) -> None:
    """Materialize the repository-level checkout left by the first install."""
    root = apm_modules / REPO
    for name in ("README.md", "src/index.ts", "docs/guide.md"):
        _write(root / name)
    _write(root / ".apm-pin", '{"schema_version": 1}')
    for skill in (
        ".claude/skills/dev",
        ".codex/skills/release",
        ".codex/skills/review",
        "skills/unrequested",
    ):
        _write(root / skill / "SKILL.md", "---\nname: s\n---\n")
    for subdir in SUBDIRS:
        _write(root / subdir / "SKILL.md", "---\nname: s\n---\n")
        _write(root / subdir / ".apm-pin", '{"schema_version": 1}')
    _write(root / "skills/a/agents/openai.yaml")


def _write_manifest(apm_dir: Path, deps: list[str]) -> None:
    lines = ["name: user", "version: 1.0.0", "dependencies:", "  apm:"]
    lines.extend(deps)
    _write(apm_dir / "apm.yml", "\n".join(lines) + "\n")


def _subdir_manifest_lines() -> list[str]:
    lines = []
    for subdir in SUBDIRS:
        lines.extend([f"    - git: {REPO}", f"      path: {subdir}"])
    return lines


def _ctx(apm_dir: Path, scope: InstallScope, previous: LockFile, current: LockFile) -> MagicMock:
    ctx = MagicMock()
    ctx.existing_lockfile = previous
    ctx.only_packages = False
    ctx.intended_dep_keys = set(current.dependencies)
    ctx.package_deployed_files = {key: [] for key in current.dependencies}
    ctx.orphan_cleanup_retained = {}
    ctx.package_cleanup_retained = {}
    ctx.project_root = apm_dir.parent if scope is InstallScope.USER else apm_dir
    ctx.apm_modules_dir = apm_dir / "apm_modules"
    ctx.deps_to_install = [dep.to_dependency_ref() for dep in current.dependencies.values()]
    ctx.dependency_graph = None
    ctx.targets = [KNOWN_TARGETS["copilot"]]
    ctx.scope = scope
    ctx.diagnostics = DiagnosticCollector()
    ctx.logger = MagicMock()
    return ctx


def _apm_dir(tmp_path: Path, scope: InstallScope, monkeypatch: pytest.MonkeyPatch) -> Path:
    if scope is InstallScope.USER:
        home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        apm_dir = home / ".apm"
    else:
        apm_dir = tmp_path / "project"
    apm_dir.mkdir(parents=True)
    monkeypatch.chdir(apm_dir)
    return apm_dir


def _files_under(apm_modules: Path) -> set[str]:
    return {p.relative_to(apm_modules).as_posix() for p in apm_modules.rglob("*") if p.is_file()}


def _prune_orphans(apm_dir: Path) -> list[str]:
    apm_modules = apm_dir / "apm_modules"
    declared = APMPackage.from_apm_yml(apm_dir / "apm.yml").get_all_apm_dependencies()
    lockfile = LockFile.read(apm_dir / "apm.lock.yaml")
    expected = _build_expected_install_paths(declared, lockfile, apm_modules)
    return _find_orphaned_packages(_scan_installed_packages(apm_modules), expected)


@pytest.mark.parametrize("scope", [InstallScope.PROJECT, InstallScope.USER])
def test_switch_to_subdirectory_deps_trims_superseded_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: InstallScope
) -> None:
    apm_dir = _apm_dir(tmp_path, scope, monkeypatch)
    apm_modules = apm_dir / "apm_modules"
    _write_whole_repo_checkout(apm_modules)
    previous = _lockfile(_whole_repo_dep())
    current = _lockfile(*(_subdir_dep(path) for path in SUBDIRS))
    _write_manifest(apm_dir, _subdir_manifest_lines())
    current.write(apm_dir / "apm.lock.yaml")

    # The reporter's state: deps list and prune flag the embedded skills.
    _installed, orphaned_before = _resolve_scope_deps(apm_dir, MagicMock())
    assert orphaned_before

    cleanup.run(_ctx(apm_dir, scope, previous, current))

    assert _files_under(apm_modules) == FRESH_SUBDIR_TREE
    installed, orphaned = _resolve_scope_deps(apm_dir, MagicMock())
    assert orphaned == []
    assert sorted(pkg["name"] for pkg in installed) == [f"{REPO}/{path}" for path in SUBDIRS]
    assert _prune_orphans(apm_dir) == []


def test_released_root_without_hosted_subdirectory_is_left_for_prune(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain removal keeps the existing contract: prune reports and removes it."""
    apm_dir = _apm_dir(tmp_path, InstallScope.PROJECT, monkeypatch)
    apm_modules = apm_dir / "apm_modules"
    _write_whole_repo_checkout(apm_modules)
    before = _files_under(apm_modules)
    previous = _lockfile(_whole_repo_dep())
    current = LockFile()
    _write_manifest(apm_dir, ["    []"])

    cleanup.run(_ctx(apm_dir, InstallScope.PROJECT, previous, current))

    assert _files_under(apm_modules) == before


def test_retained_whole_repo_package_is_never_trimmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A released subdirectory entry never trims a package that is still declared."""
    apm_dir = _apm_dir(tmp_path, InstallScope.PROJECT, monkeypatch)
    apm_modules = apm_dir / "apm_modules"
    _write_whole_repo_checkout(apm_modules)
    before = _files_under(apm_modules)
    previous = _lockfile(_whole_repo_dep(), _subdir_dep("skills"))
    current = _lockfile(_whole_repo_dep(), _subdir_dep("skills/a"))

    cleanup.run(_ctx(apm_dir, InstallScope.PROJECT, previous, current))

    assert _files_under(apm_modules) == before


@pytest.mark.parametrize("scope", [InstallScope.PROJECT, InstallScope.USER])
def test_repo_level_install_lists_embedded_skills_as_bundled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: InstallScope
) -> None:
    """Before the switch, deps list must agree with prune on a whole-repo bundle."""
    apm_dir = _apm_dir(tmp_path, scope, monkeypatch)
    _write_whole_repo_checkout(apm_dir / "apm_modules")
    _write_manifest(apm_dir, [f"    - {REPO}"])
    _lockfile(_whole_repo_dep()).write(apm_dir / "apm.lock.yaml")

    installed, orphaned = _resolve_scope_deps(apm_dir, MagicMock())

    assert installed
    assert orphaned == []
    assert _prune_orphans(apm_dir) == []


def test_undeclared_standalone_parent_of_declared_subdirectory_stays_orphaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bundled descendants are kept; a real undeclared parent package is still reported."""
    apm_dir = _apm_dir(tmp_path, InstallScope.PROJECT, monkeypatch)
    apm_modules = apm_dir / "apm_modules"
    _write(apm_modules / REPO / "apm.yml", "name: repo\nversion: 1.0.0\n")
    _write(apm_modules / REPO / "skills/a/SKILL.md", "---\nname: s\n---\n")
    _write_manifest(apm_dir, [f"    - git: {REPO}", "      path: skills/a"])
    _lockfile(_subdir_dep("skills/a")).write(apm_dir / "apm.lock.yaml")

    _installed, orphaned = _resolve_scope_deps(apm_dir, MagicMock())

    assert orphaned == [REPO]


def _case_sensitive(directory: Path) -> bool:
    probe = directory / "case-probe"
    probe.mkdir(parents=True)
    try:
        return not (directory / "CASE-PROBE").exists()
    finally:
        probe.rmdir()


def _cli(apm_dir: Path, *args: str) -> str:
    try:
        result = CliRunner().invoke(cli, list(args), catch_exceptions=False)
    finally:
        clear_apm_yml_cache()
    assert result.exit_code == 0, result.output
    return result.output


def test_switch_trims_case_distinct_entries_on_case_sensitive_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every case-distinct sibling of superseded content is trimmed, not just one."""
    apm_dir = _apm_dir(tmp_path, InstallScope.PROJECT, monkeypatch)
    apm_modules = apm_dir / "apm_modules"
    if not _case_sensitive(tmp_path):
        pytest.skip("needs a case-sensitive filesystem")
    _write_whole_repo_checkout(apm_modules)
    _write(apm_modules / REPO / "readme.md")
    _write(apm_modules / REPO / "Docs/guide.md")
    previous = _lockfile(_whole_repo_dep())
    current = _lockfile(*(_subdir_dep(path) for path in SUBDIRS))

    cleanup.run(_ctx(apm_dir, InstallScope.PROJECT, previous, current))

    assert _files_under(apm_modules) == FRESH_SUBDIR_TREE


def test_declared_bundle_with_changed_repo_casing_lists_skills_as_bundled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """deps list reuses prune's installed-case lookup for a re-cased declaration."""
    apm_dir = _apm_dir(tmp_path, InstallScope.PROJECT, monkeypatch)
    _write_whole_repo_checkout(apm_dir / "apm_modules")
    _write_manifest(apm_dir, ["    - Owner/Repo"])

    _installed, orphaned = _resolve_scope_deps(apm_dir, MagicMock())

    assert orphaned == []
    assert "No orphaned packages found" in _cli(apm_dir, "prune", "--dry-run")


def test_undeclared_parent_deeper_than_repo_root_stays_orphaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real-orphan warning holds for a parent package at any depth."""
    apm_dir = _apm_dir(tmp_path, InstallScope.PROJECT, monkeypatch)
    apm_modules = apm_dir / "apm_modules"
    _write(apm_modules / REPO / "skills/parent/SKILL.md", "---\nname: p\n---\n")
    _write(apm_modules / REPO / "skills/parent/child/SKILL.md", "---\nname: c\n---\n")
    _write_manifest(apm_dir, [f"    - git: {REPO}", "      path: skills/parent/child"])
    _lockfile(_subdir_dep("skills/parent/child")).write(apm_dir / "apm.lock.yaml")

    _installed, orphaned = _resolve_scope_deps(apm_dir, MagicMock())

    assert orphaned == [f"{REPO}/skills/parent"]


def test_deps_list_falls_back_to_declared_paths_when_selector_inputs_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If expected paths cannot be built, deps list keeps its conservative rule."""
    apm_dir = _apm_dir(tmp_path, InstallScope.PROJECT, monkeypatch)
    _write_whole_repo_checkout(apm_dir / "apm_modules")
    _write_manifest(apm_dir, [f"    - {REPO}"])
    _lockfile(_whole_repo_dep()).write(apm_dir / "apm.lock.yaml")
    monkeypatch.setattr(
        deps_cli, "_build_expected_install_paths", MagicMock(side_effect=ValueError("boom"))
    )

    installed, orphaned = _resolve_scope_deps(apm_dir, MagicMock())

    assert len(installed) == len(orphaned) == 6
    assert all(name.startswith(f"{REPO}/") for name in orphaned)


@pytest.mark.parametrize("scope", [InstallScope.PROJECT, InstallScope.USER])
def test_cli_deps_list_and_prune_agree_after_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: InstallScope
) -> None:
    """Through the CLI, deps list, deps tree and prune agree once the switch is cleaned."""
    apm_dir = _apm_dir(tmp_path, scope, monkeypatch)
    _write_whole_repo_checkout(apm_dir / "apm_modules")
    previous = _lockfile(_whole_repo_dep())
    current = _lockfile(*(_subdir_dep(path) for path in SUBDIRS))
    _write_manifest(apm_dir, _subdir_manifest_lines())
    current.write(apm_dir / "apm.lock.yaml")
    flag = ["-g"] if scope is InstallScope.USER else []
    assert "orphaned" in _cli(apm_dir, "deps", "list", *flag).lower()

    cleanup.run(_ctx(apm_dir, scope, previous, current))

    # The reporter ran prune from the metadata root, ~/.apm for user scope.
    pruned = _cli(apm_dir, "prune", "--dry-run")
    if scope is InstallScope.USER:
        monkeypatch.chdir(tmp_path)
    listed = _cli(apm_dir, "deps", "list", *flag)
    tree = _cli(apm_dir, "deps", "tree", *flag)

    assert "No orphaned packages found" in pruned
    assert "orphaned" not in listed.lower()
    for path in SUBDIRS:
        assert f"{REPO}/{path}" in listed
        assert f"{REPO}/{path}" in tree
