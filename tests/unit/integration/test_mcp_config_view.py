"""Tests for the canonical current MCP configuration view."""

from __future__ import annotations

from pathlib import Path

import yaml

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.integration.mcp_config_view import CurrentMcpConfigView, McpConfigDiff
from apm_cli.models.apm_package import APMPackage, clear_apm_yml_cache
from apm_cli.models.dependency.mcp import MCPDependency


def _write_manifest(
    directory: Path,
    *,
    name: str,
    mcp: list[dict[str, object] | str] | None = None,
    dev_mcp: list[dict[str, object] | str] | None = None,
) -> APMPackage:
    """Write and parse a minimal APM package manifest."""
    directory.mkdir(parents=True, exist_ok=True)
    data: dict[str, object] = {"name": name, "version": "1.0.0"}
    if mcp is not None:
        data["dependencies"] = {"mcp": mcp}
    if dev_mcp is not None:
        data["devDependencies"] = {"mcp": dev_mcp}
    path = directory / "apm.yml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    clear_apm_yml_cache()
    return APMPackage.from_apm_yml(path)


def _self_defined(name: str, command: str) -> dict[str, object]:
    """Return a valid self-defined stdio MCP declaration."""
    return {
        "name": name,
        "registry": False,
        "transport": "stdio",
        "command": command,
    }


def _lock(*dependencies: LockedDependency) -> LockFile:
    """Build a lockfile preserving the supplied dependency order."""
    return LockFile(dependencies={dep.get_unique_key(): dep for dep in dependencies})


def _derive(
    root: APMPackage, lockfile: LockFile | None, modules_root: Path
) -> CurrentMcpConfigView:
    """Derive a view with transitive self-defined declarations trusted."""
    return CurrentMcpConfigView.derive(
        root,
        lockfile,
        modules_root,
        trust_transitive_self_defined=True,
    )


def test_derives_root_and_package_production_and_dev_mcp(tmp_path: Path) -> None:
    """Root and locked package prod/dev declarations share manifest order."""
    root = _write_manifest(
        tmp_path,
        name="root",
        mcp=["root-prod"],
        dev_mcp=["root-dev"],
    )
    package_dir = tmp_path / "packages" / "tools"
    _write_manifest(
        package_dir,
        name="tools",
        mcp=["package-prod"],
        dev_mcp=["package-dev"],
    )
    locked = LockedDependency(
        repo_url="_local/tools",
        source="local",
        local_path="./packages/tools",
        depth=1,
    )

    view = _derive(root, _lock(locked), tmp_path / "apm_modules")

    assert [dep.name for dep in view.dependencies] == [
        "root-prod",
        "root-dev",
        "package-prod",
        "package-dev",
    ]
    assert view.provenance == {
        "package-prod": "tools",
        "package-dev": "tools",
    }


def test_derives_local_and_installed_remote_lock_bounded_manifests(tmp_path: Path) -> None:
    """Local sources and installed remote sources use their canonical paths."""
    root = _write_manifest(tmp_path, name="root")
    local_dir = tmp_path / "packages" / "local-tools"
    _write_manifest(local_dir, name="local-tools", mcp=["local-server"])

    modules_root = tmp_path / "apm_modules"
    remote = LockedDependency(repo_url="owner/remote-tools", depth=1)
    remote_dir = remote.to_dependency_ref().get_install_path(modules_root)
    _write_manifest(remote_dir, name="remote-tools", mcp=["remote-server"])
    local = LockedDependency(
        repo_url="_local/local-tools",
        source="local",
        local_path="./packages/local-tools",
        depth=1,
    )

    view = _derive(root, _lock(local, remote), modules_root)

    assert [dep.name for dep in view.dependencies] == ["local-server", "remote-server"]
    assert view.provenance == {
        "local-server": "local-tools",
        "remote-server": "remote-tools",
    }
    assert view.problems == ()


def test_root_declaration_wins_duplicate_name(tmp_path: Path) -> None:
    """Root-first first-wins dedup keeps root config and no provenance."""
    root = _write_manifest(
        tmp_path,
        name="root",
        mcp=[_self_defined("shared", "root-command")],
    )
    package_dir = tmp_path / "packages" / "tools"
    _write_manifest(
        package_dir,
        name="tools",
        mcp=[
            _self_defined("shared", "package-command"),
            _self_defined("unique", "unique-command"),
        ],
    )
    locked = LockedDependency(
        repo_url="_local/tools",
        source="local",
        local_path="./packages/tools",
        depth=1,
    )

    view = _derive(root, _lock(locked), tmp_path / "apm_modules")

    assert [dep.name for dep in view.dependencies] == ["shared", "unique"]
    assert view.configs["shared"]["command"] == "root-command"
    assert view.provenance == {"unique": "tools"}


def test_symmetric_diff_reports_changed_source_only_and_lock_only() -> None:
    """Diff partitions changed and one-sided server names."""
    diff = McpConfigDiff.between(
        {
            "changed": {"name": "changed", "command": "new"},
            "source-only": {"name": "source-only"},
            "same": {"name": "same"},
        },
        {
            "changed": {"name": "changed", "command": "old"},
            "lock-only": {"name": "lock-only"},
            "same": {"name": "same"},
        },
    )

    assert diff.changed == frozenset({"changed"})
    assert diff.source_only == frozenset({"source-only"})
    assert diff.lock_only == frozenset({"lock-only"})
    assert not diff.is_empty
    assert McpConfigDiff.between({}, {}).is_empty


def test_provenance_never_exempts_lock_only_name() -> None:
    """Historical provenance cannot prove a current declaration exists."""
    view = CurrentMcpConfigView(
        dependencies=(),
        configs={},
        provenance={"removed": "old-package"},
        problems=(),
    )

    diff = view.diff({"removed": {"name": "removed"}})

    assert diff.lock_only == frozenset({"removed"})


def test_local_package_config_change_is_detected(tmp_path: Path) -> None:
    """Rehome PR 2132: local package config is compared with its baseline."""
    root = _write_manifest(tmp_path, name="root")
    package_dir = tmp_path / "packages" / "agent-config"
    _write_manifest(
        package_dir,
        name="agent-config",
        mcp=[_self_defined("shadcn", "changed")],
    )
    locked = LockedDependency(
        repo_url="_local/agent-config",
        source="local",
        local_path="./packages/agent-config",
        depth=1,
    )
    view = _derive(root, _lock(locked), tmp_path / "apm_modules")

    diff = view.diff(
        {
            "shadcn": {
                "name": "shadcn",
                "registry": False,
                "transport": "stdio",
                "command": "ready",
            }
        }
    )

    assert diff.changed == frozenset({"shadcn"})


def test_removed_local_and_remote_declarations_are_lock_only(tmp_path: Path) -> None:
    """Rehome PR 2145: removed declarations are symmetric for both source kinds."""
    root = _write_manifest(tmp_path, name="root")
    local_dir = tmp_path / "packages" / "local-tools"
    _write_manifest(local_dir, name="local-tools")
    modules_root = tmp_path / "apm_modules"
    remote = LockedDependency(repo_url="owner/remote-tools", depth=1)
    _write_manifest(
        remote.to_dependency_ref().get_install_path(modules_root),
        name="remote-tools",
    )
    local = LockedDependency(
        repo_url="_local/local-tools",
        source="local",
        local_path="./packages/local-tools",
        depth=1,
    )

    view = _derive(root, _lock(local, remote), modules_root)
    diff = view.diff(
        {
            "local-removed": {"name": "local-removed"},
            "remote-removed": {"name": "remote-removed"},
        }
    )

    assert diff.lock_only == frozenset({"local-removed", "remote-removed"})


def test_missing_package_manifest_records_problem(tmp_path: Path) -> None:
    """A locked package missing apm.yml cannot yield a vacuous pass."""
    root = _write_manifest(tmp_path, name="root")
    locked = LockedDependency(
        repo_url="_local/missing",
        source="local",
        local_path="./packages/missing",
        depth=1,
    )

    view = _derive(root, _lock(locked), tmp_path / "apm_modules")

    assert len(view.problems) == 1
    problem = view.problems[0]
    assert problem.package_key == locked.get_unique_key()
    assert problem.manifest_path == (tmp_path / "packages" / "missing" / "apm.yml").resolve()
    assert "manifest not found" in problem.message


def test_invalid_local_and_remote_manifests_record_problems(tmp_path: Path) -> None:
    """Rehome PRs 2132/2145: parse errors identify both package sources."""
    root = _write_manifest(tmp_path, name="root")
    local = LockedDependency(
        repo_url="_local/broken",
        source="local",
        local_path="./packages/broken",
        depth=1,
    )
    local_path = tmp_path / "packages" / "broken"
    local_path.mkdir(parents=True)
    (local_path / "apm.yml").write_text("name: [invalid\n", encoding="utf-8")

    modules_root = tmp_path / "apm_modules"
    remote = LockedDependency(repo_url="owner/broken", depth=1)
    remote_path = remote.to_dependency_ref().get_install_path(modules_root)
    remote_path.mkdir(parents=True)
    (remote_path / "apm.yml").write_text("name: [invalid\n", encoding="utf-8")

    view = _derive(root, _lock(local, remote), modules_root)

    assert [problem.package_key for problem in view.problems] == [
        local.get_unique_key(),
        remote.get_unique_key(),
    ]
    assert all("cannot parse package manifest" in problem.message for problem in view.problems)


def test_local_resolution_error_records_problem(tmp_path: Path) -> None:
    """Rehome PR 2132: a corrupt local lock graph is reported, not raised."""
    root = _write_manifest(tmp_path, name="root")
    locked = LockedDependency(
        repo_url="_local/child",
        source="local",
        local_path="../child",
        resolved_by="_local/missing-parent",
        depth=2,
    )

    view = _derive(root, _lock(locked), tmp_path / "apm_modules")

    assert len(view.problems) == 1
    assert view.problems[0].package_key == locked.get_unique_key()
    assert "cannot resolve local package" in view.problems[0].message


def test_manifestless_skill_bundle_is_skipped(tmp_path: Path) -> None:
    """Manifestless bundles cannot declare MCP and do not create problems."""
    root = _write_manifest(tmp_path, name="root")
    bundle = tmp_path / "skills" / "bundle"
    bundle.mkdir(parents=True)
    (bundle / "SKILL.md").write_text("# Bundle\n", encoding="utf-8")
    locked = LockedDependency(
        repo_url="_local/bundle",
        source="local",
        local_path="./skills/bundle",
        package_type="skill_bundle",
        depth=1,
    )

    view = _derive(root, _lock(locked), tmp_path / "apm_modules")

    assert view.dependencies == ()
    assert view.problems == ()


def test_manifestless_virtual_package_is_skipped(tmp_path: Path) -> None:
    """Virtual (git+path) skills have no apm.yml by design and must not fail."""
    root = _write_manifest(tmp_path, name="root")
    modules_root = tmp_path / "apm_modules"
    locked = LockedDependency(
        repo_url="angular/skills",
        virtual_path="angular-developer",
        is_virtual=True,
        package_type="claude_skill",
        depth=1,
    )
    skill_dir = locked.to_dependency_ref().get_install_path(modules_root)
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Angular Developer\n", encoding="utf-8")

    view = _derive(root, _lock(locked), modules_root)

    assert not (skill_dir / "apm.yml").exists()
    assert view.dependencies == ()
    assert view.problems == ()


def test_stale_directory_absent_from_lockfile_is_never_scanned(tmp_path: Path) -> None:
    """Only lockfile entries bound package-manifest traversal."""
    root = _write_manifest(tmp_path, name="root", mcp=["root-server"])
    stale = tmp_path / "apm_modules" / "stale" / "package"
    stale.mkdir(parents=True)
    (stale / "apm.yml").write_text("name: [invalid\n", encoding="utf-8")

    view = _derive(root, LockFile(), tmp_path / "apm_modules")

    assert [dep.name for dep in view.dependencies] == ["root-server"]
    assert view.problems == ()


def test_transitive_self_defined_trust_matches_install_behavior(tmp_path: Path) -> None:
    """Depth-one servers are trusted; deeper servers require explicit trust."""
    root = _write_manifest(tmp_path, name="root")
    direct_dir = tmp_path / "packages" / "direct"
    deep_dir = tmp_path / "packages" / "deep"
    _write_manifest(direct_dir, name="direct", mcp=[_self_defined("direct-server", "echo")])
    _write_manifest(deep_dir, name="deep", mcp=[_self_defined("deep-server", "echo")])
    direct = LockedDependency(
        repo_url="_local/direct",
        source="local",
        local_path="./packages/direct",
        depth=1,
    )
    deep = LockedDependency(
        repo_url="_local/deep",
        source="local",
        local_path="./packages/deep",
        depth=2,
    )
    lockfile = _lock(direct, deep)

    denied = CurrentMcpConfigView.derive(
        root,
        lockfile,
        tmp_path / "apm_modules",
        trust_transitive_self_defined=False,
    )
    trusted = _derive(root, lockfile, tmp_path / "apm_modules")

    assert [dep.name for dep in denied.dependencies] == ["direct-server"]
    assert [dep.name for dep in trusted.dependencies] == ["direct-server", "deep-server"]


def test_view_dependencies_are_mcp_dependency_objects(tmp_path: Path) -> None:
    """The public dependencies tuple remains strongly typed."""
    root = _write_manifest(tmp_path, name="root", mcp=["server"])

    view = _derive(root, None, tmp_path / "apm_modules")

    assert all(isinstance(dep, MCPDependency) for dep in view.dependencies)
