"""Tests for the canonical current MCP configuration view."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.integration.mcp_config_view import (
    CurrentMcpConfigView,
    McpConfigDiff,
    _collect_transitive_compat,
)
from apm_cli.models.apm_package import APMPackage, clear_apm_yml_cache
from apm_cli.models.dependency.mcp import MCPDependency
from tests.unit.copilot_plugins._builders import write_agent_plugin
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged

pytestmark = pytest.mark.component


@pytest.mark.parametrize("include_dependency_mcp,depth", [(False, 1), (True, 2)])
def test_dependency_omission_requires_bound_selection_evidence(
    tmp_path: Path, include_dependency_mcp: bool, depth: int
) -> None:
    """Only the exact deliberately omitted declaration is exempt from comparison."""
    root = _write_manifest(tmp_path, name="consumer")
    source = tmp_path / "package"
    declaration = _self_defined("firebase", "inert")
    _write_manifest(source, name="firebase", mcp=[declaration])
    dependency = LockedDependency(
        repo_url="_local/firebase", source="local", local_path=str(source), depth=depth
    )
    lock = _lock(dependency)
    modules = tmp_path / "apm_modules"
    selected = CurrentMcpConfigView.derive(
        root,
        lock,
        modules,
        trust_transitive_self_defined=False,
        include_dependency_mcp=include_dependency_mcp,
    )
    assert selected.dependencies == ()
    lock.mcp_omissions = selected.omission_evidence(lock)
    assert set(lock.mcp_omissions) == {"firebase"}
    lock = LockFile.from_yaml(lock.to_yaml())
    audited = _derive(root, lock, modules).with_recorded_omissions(lock)
    assert audited.diff(lock.mcp_configs).is_empty
    assert not audited.problems

    _write_manifest(source, name="firebase", mcp=[_self_defined("firebase", "changed")])
    changed = _derive(root, lock, modules).with_recorded_omissions(lock)
    assert changed.diff(lock.mcp_configs).source_only == {"firebase"}
    assert changed.problems


@pytest.mark.parametrize(
    "damage",
    [
        "lost",
        "empty",
        "reason",
        "malformed-reason",
        "dependency",
        "baseline",
        "server-only",
        "provenance-only",
        "ownership-only",
        "root",
    ],
)
def test_omission_never_masks_unknown_or_previously_admitted_state(
    tmp_path: Path, damage: str
) -> None:
    """An empty field, stale identity, root or previous baseline cannot grant omission."""
    root = _write_manifest(tmp_path, name="consumer")
    source = tmp_path / "package"
    declaration = _self_defined("firebase", "inert")
    _write_manifest(source, name="firebase", mcp=[declaration])
    dependency = LockedDependency(
        repo_url="_local/firebase", source="local", local_path=str(source), depth=1
    )
    lock = _lock(dependency)
    modules = tmp_path / "apm_modules"
    selected = CurrentMcpConfigView.derive(
        root, lock, modules, trust_transitive_self_defined=False, include_dependency_mcp=False
    )
    lock.mcp_omissions = selected.omission_evidence(lock)
    if damage == "lost":
        lock.mcp_omissions = {}
    elif damage == "empty":
        lock.mcp_omissions["firebase"] = {}
    elif damage == "reason":
        lock.mcp_omissions["firebase"]["reason"] = "unknown"
    elif damage == "malformed-reason":
        lock.mcp_omissions["firebase"]["reason"] = []
    elif damage == "dependency":
        lock.dependencies.clear()
        different_source = tmp_path / "different-package"
        _write_manifest(different_source, name="firebase", mcp=[declaration])
        dependency.local_path = str(different_source)
        lock.add_dependency(dependency)
    elif damage == "baseline":
        lock.mcp_servers = ["firebase"]
        lock.mcp_configs = {"firebase": _self_defined("firebase", "old")}
    elif damage == "server-only":
        lock.mcp_servers = ["firebase"]
    elif damage == "provenance-only":
        lock.mcp_config_provenance = {"firebase": "package"}
    elif damage == "ownership-only":
        lock.mcp_target_servers = {"claude": ["firebase"]}
    else:
        root = _write_manifest(tmp_path, name="consumer", mcp=[declaration])
    audited = _derive(root, lock, modules).with_recorded_omissions(lock)
    assert not audited.diff(lock.mcp_configs).is_empty


@pytest.mark.parametrize("root_declared", [False, True])
def test_admitted_config_supersedes_obsolete_omission(tmp_path: Path, root_declared: bool) -> None:
    """Old omission evidence cannot invalidate admission or hide later real drift."""
    root = _write_manifest(tmp_path, name="consumer")
    source = tmp_path / "package"
    declaration = _self_defined("firebase", "inert")
    _write_manifest(source, name="firebase", mcp=[declaration])
    lock = _lock(
        LockedDependency(
            repo_url="_local/firebase", source="local", local_path=str(source), depth=1
        )
    )
    modules = tmp_path / "apm_modules"
    selected = CurrentMcpConfigView.derive(
        root, lock, modules, trust_transitive_self_defined=False, include_dependency_mcp=False
    )
    lock.mcp_omissions = selected.omission_evidence(lock)
    assert set(lock.mcp_omissions) == {"firebase"}
    if root_declared:
        root = _write_manifest(tmp_path, name="consumer", mcp=[declaration])
    lock.mcp_configs = dict(_derive(root, lock, modules).configs)
    lock.mcp_servers = ["firebase"]
    before = lock.to_yaml()
    audited = _derive(root, lock, modules).with_recorded_omissions(lock)
    assert audited.problems == ()
    assert audited.configs == lock.mcp_configs
    assert audited.diff(lock.mcp_configs).is_empty
    assert lock.to_yaml() == before

    changed = [_self_defined("firebase", "changed")]
    if root_declared:
        root = _write_manifest(tmp_path, name="consumer", mcp=changed)
    else:
        _write_manifest(source, name="firebase", mcp=changed)
    audited = _derive(root, lock, modules).with_recorded_omissions(lock)
    assert audited.diff(lock.mcp_configs).changed == {"firebase"}


@pytest.mark.parametrize("field", ["args", "env"])
def test_omission_fingerprint_uses_authored_portable_declaration(
    tmp_path: Path, field: str
) -> None:
    """Replay relocation is harmless; changing an authored literal is not."""
    root = _write_manifest(tmp_path, name="consumer")
    dependency = LockedDependency(
        repo_url="owner/firebase", resolved_ref="main", resolved_commit="a" * 40, depth=1
    )
    lock = _lock(dependency)
    installed = tmp_path / "installed"
    replay = tmp_path / "replay"
    source = dependency.to_dependency_ref().get_install_path(installed)
    replay_source = dependency.to_dependency_ref().get_install_path(replay)
    declaration = _self_defined("firebase", "inert")
    portable = "${CLAUDE_PLUGIN_ROOT}/start.mjs"
    declaration[field] = [portable] if field == "args" else {"SCRIPT": portable}
    _write_manifest(source, name="firebase", mcp=[declaration])
    _write_manifest(replay_source, name="firebase", mcp=[declaration])
    selected = CurrentMcpConfigView.derive(
        root, lock, installed, trust_transitive_self_defined=False, include_dependency_mcp=False
    )
    lock.mcp_omissions = selected.omission_evidence(lock)
    assert set(lock.mcp_omissions) == {"firebase"}
    installed_view = _derive(root, lock, installed)
    replay_view = _derive(root, lock, replay)
    assert installed_view.problems == replay_view.problems == ()
    assert installed_view.configs["firebase"][field] != replay_view.configs["firebase"][field]
    assert replay_view.declaration_fingerprints == installed_view.declaration_fingerprints
    audited = replay_view.with_recorded_omissions(lock)
    assert audited.configs == {}
    assert audited.problems == ()

    literal = str(source.resolve() / "start.mjs")
    declaration[field] = [literal] if field == "args" else {"SCRIPT": literal}
    _write_manifest(source, name="firebase", mcp=[declaration])
    selected = CurrentMcpConfigView.derive(
        root, lock, installed, trust_transitive_self_defined=False, include_dependency_mcp=False
    )
    lock.mcp_omissions = selected.omission_evidence(lock)
    other_literal = str(replay_source.resolve() / "start.mjs")
    declaration[field] = [other_literal] if field == "args" else {"SCRIPT": other_literal}
    _write_manifest(replay_source, name="firebase", mcp=[declaration])
    changed = _derive(root, lock, replay).with_recorded_omissions(lock)
    assert set(changed.configs) == {"firebase"}
    assert [problem.package_key for problem in changed.problems] == ["firebase"]


def test_pending_sources_preserve_known_omission_evidence(tmp_path: Path) -> None:
    """The merged collector retains pending and omission metadata independently."""
    root = _write_manifest(tmp_path, name="consumer")
    source = tmp_path / "package"
    _write_manifest(source, name="firebase", mcp=[_self_defined("firebase", "inert")])
    known = LockedDependency(
        repo_url="_local/firebase", source="local", local_path=str(source), depth=1
    )
    cold = LockedDependency(
        repo_url="owner/cold",
        name="cold",
        resolved_ref="main",
        resolved_commit="a" * 40,
        package_type="apm_package",
        depth=1,
    )
    lock = _lock(known, cold)
    selected = CurrentMcpConfigView.derive(
        root,
        lock,
        tmp_path / "apm_modules",
        trust_transitive_self_defined=False,
        include_dependency_mcp=False,
        allow_pending=True,
        active_package_keys=frozenset(lock.dependencies),
    )
    assert selected.problems == ()
    assert selected.pending_packages == {cold.get_unique_key()}
    assert set(selected.declaration_fingerprints) == {"firebase"}
    assert selected.omission_evidence(lock) == {
        "firebase": {
            "declaration": selected.declaration_fingerprints["firebase"],
            "reason": "only-apm",
        }
    }
    assert selected.configs == {}


@pytest.mark.parametrize("include_dependency_mcp,depth", [(False, 1), (True, 2)])
def test_omission_and_compat_views_preserve_nested_single_file_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    include_dependency_mcp: bool,
    depth: int,
) -> None:
    """A sibling primitive cannot lose omission evidence or become an MCP manifest."""
    root = _write_manifest(tmp_path / "consumer", name="consumer")
    parent = tmp_path / "package"
    _write_manifest(parent, name="firebase", mcp=[_self_defined("firebase", "inert")])
    source = tmp_path / "selected.agent.md"
    source.write_bytes(
        b"---\ndescription: Inert primitive\ndependencies:\n  mcp: [not-a-manifest]\n"
        b"---\n# Guidance\n"
    )
    package = LockedDependency(
        repo_url="_local/firebase", source="local", local_path="../package", depth=depth
    )
    primitive = LockedDependency(
        repo_url=f"_local/{source.name}",
        source="local",
        local_path=f"../{source.name}",
        package_type="apm_package",
        depth=depth + 1,
        resolved_by=package.repo_url,
        declaring_parent="../package",
        anchored_local_path=source.as_posix(),
    )
    lock = _lock(package, primitive)
    modules = root.package_path / "apm_modules"
    modules.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr("apm_cli.config.get_apm_temp_dir", lambda: str(scratch))
    selected = CurrentMcpConfigView.derive(
        root,
        lock,
        modules,
        trust_transitive_self_defined=False,
        include_dependency_mcp=include_dependency_mcp,
        allow_pending=False,
    )
    assert selected.problems == ()
    assert selected.configs == {}
    lock.mcp_omissions = selected.omission_evidence(lock)
    assert set(lock.mcp_omissions) == {"firebase"}
    assert set(selected.declaration_fingerprints) == {"firebase"}
    lock_path = root.package_path / "apm.lock.yaml"
    lock.write(lock_path)
    before = ArtifactSnapshot.capture(tmp_path)
    audited = _derive(root, lock, modules).with_recorded_omissions(lock)
    assert audited.problems == ()
    assert audited.configs == {}
    dependencies = _collect_transitive_compat(
        modules, lock_path, True, logger=None, diagnostics=None
    )
    assert [dependency.name for dependency in dependencies] == ["firebase"]
    assert list(scratch.iterdir()) == []
    assert_unchanged(before, ArtifactSnapshot.capture(tmp_path))


def test_omission_declaration_and_config_share_one_manifest_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fingerprint and expanded config must describe the same manifest snapshot."""
    from apm_cli.utils import yaml_io

    root = _write_manifest(tmp_path, name="consumer")
    source = tmp_path / "package"
    _write_manifest(source, name="package", mcp=[_self_defined("service", "inert")])
    lock = _lock(
        LockedDependency(repo_url="_local/package", source="local", local_path=str(source))
    )
    reads: list[Path] = []
    original = yaml_io.load_yaml

    def counted_load(path: Path) -> object:
        reads.append(path)
        return original(path)

    monkeypatch.setattr(yaml_io, "load_yaml", counted_load)
    view = _derive(root, lock, tmp_path / "apm_modules")
    assert view.problems == ()
    assert view.configs["service"]["command"] == "inert"
    assert set(view.declaration_fingerprints) == {"service"}
    assert reads == [source / "apm.yml"]


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


@pytest.mark.parametrize(
    "record_key,package_type,expected_problem",
    [
        ("matching", "marketplace_plugin", False),
        ("foreign", "marketplace_plugin", True),
        ("absent", "marketplace_plugin", True),
        ("matching", "hook_package", True),
    ],
)
def test_cold_codex_cache_uses_owned_identity_without_waiving_legacy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_key: str,
    package_type: str,
    expected_problem: bool,
) -> None:
    from dataclasses import replace

    from apm_cli.codex_plugins import registrar
    from apm_cli.codex_plugins.registrar import _Record, _write_records, ledger_path_for
    from apm_cli.codex_plugins.snapshot import NativeSnapshot

    root = _write_manifest(tmp_path, name="consumer", mcp=[_self_defined("loose", "inert")])
    dependency = LockedDependency(
        name="example",
        version="0.0.0",
        repo_url="owner/example",
        resolved_ref="main",
        resolved_commit="a" * 40,
        package_type=package_type,
    )
    modules = tmp_path / "apm_modules"
    second = replace(dependency, repo_url="owner/second")
    key = dependency.get_unique_key()
    journal = ledger_path_for(modules)
    if record_key != "absent":
        snapshot = NativeSnapshot(journal.parent / "snapshots/owned", "a" * 40, "b" * 40)
        record = _Record("example", "sha256:" + "c" * 64, snapshot, tmp_path / ".codex/config.toml")
        records = (
            {key: record, second.get_unique_key(): record}
            if record_key == "matching"
            else {"foreign": record}
        )
        _write_records(journal, records)
    reads: list[Path] = []
    read_records = registrar._read_records

    def read_journal(path: Path) -> dict[str, _Record]:
        reads.append(path)
        return read_records(path)

    monkeypatch.setattr(registrar, "_read_records", read_journal)
    lockfile = _lock(dependency, second)
    view = _derive(root, lockfile, modules)
    assert bool(view.problems) is expected_problem
    assert list(view.configs) == ["loose"]
    assert not modules.exists()
    assert len(reads) == (1 if package_type == "marketplace_plugin" else 0)
    if not expected_problem:
        _write_records(journal, {})
        assert _derive(root, lockfile, modules).problems
        assert len(reads) == 2


class _RecordingLogger:
    """Capture compatibility-path verbose diagnostics."""

    def __init__(self) -> None:
        self.details: list[str] = []
        self.progress_messages: list[str] = []
        self.warning_messages: list[str] = []

    def progress(self, message: str) -> None:
        """Record one progress diagnostic."""
        self.progress_messages.append(message)

    def warning(self, message: str) -> None:
        """Record one warning diagnostic."""
        self.warning_messages.append(message)

    def verbose_detail(self, message: str) -> None:
        """Record one verbose diagnostic."""
        self.details.append(message)


def test_root_dev_mcp_included_dep_dev_mcp_excluded(tmp_path: Path) -> None:
    """Root dev MCP is included; locked package dev MCP is excluded (#2340)."""
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

    # Root dev MCP is included (authoring project); dep dev MCP is NOT (#2340).
    assert [dep.name for dep in view.dependencies] == [
        "root-prod",
        "root-dev",
        "package-prod",
    ]
    assert view.provenance == {
        "package-prod": "tools",
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


def test_manifestless_virtual_hook_package_is_skipped(tmp_path: Path) -> None:
    """A matching virtual hook-package shape legitimately omits apm.yml."""
    root = _write_manifest(tmp_path, name="root")
    modules_root = tmp_path / "apm_modules"
    locked = LockedDependency(
        repo_url="owner/hooks",
        virtual_path="packages/git-hooks",
        is_virtual=True,
        package_type="hook_package",
        depth=1,
    )
    package_dir = locked.to_dependency_ref().get_install_path(modules_root)
    hooks_dir = package_dir / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks.json").write_text('{"hooks": {}}\n', encoding="ascii")

    view = _derive(root, _lock(locked), modules_root)

    assert not (package_dir / "apm.yml").exists()
    assert view.dependencies == ()
    assert view.problems == ()


def test_missing_virtual_hook_package_records_problem(tmp_path: Path) -> None:
    """Lock metadata alone cannot waive an entirely absent hook package."""
    root = _write_manifest(tmp_path, name="root")
    modules_root = tmp_path / "apm_modules"
    locked = LockedDependency(
        repo_url="owner/hooks",
        virtual_path="packages/git-hooks",
        is_virtual=True,
        package_type="hook_package",
        depth=1,
    )
    package_dir = locked.to_dependency_ref().get_install_path(modules_root)

    view = _derive(root, _lock(locked), modules_root)

    assert not package_dir.exists()
    assert len(view.problems) == 1
    assert "manifest not found" in view.problems[0].message


def test_manifestless_hook_package_requires_virtual_subdirectory(tmp_path: Path) -> None:
    """A hook shape alone cannot waive the manifest for a non-virtual package."""
    root = _write_manifest(tmp_path, name="root")
    modules_root = tmp_path / "apm_modules"
    locked = LockedDependency(
        repo_url="owner/hooks",
        package_type="hook_package",
        depth=1,
    )
    package_dir = locked.to_dependency_ref().get_install_path(modules_root)
    hooks_dir = package_dir / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks.json").write_text('{"hooks": {}}\n', encoding="ascii")

    view = _derive(root, _lock(locked), modules_root)

    assert len(view.problems) == 1
    assert "manifest not found" in view.problems[0].message


def test_manifestless_virtual_hook_package_requires_matching_lock_type(
    tmp_path: Path,
) -> None:
    """A detected virtual hook package cannot waive mismatched lock metadata."""
    root = _write_manifest(tmp_path, name="root")
    modules_root = tmp_path / "apm_modules"
    locked = LockedDependency(
        repo_url="owner/hooks",
        virtual_path="packages/git-hooks",
        is_virtual=True,
        package_type="claude_skill",
        depth=1,
    )
    package_dir = locked.to_dependency_ref().get_install_path(modules_root)
    hooks_dir = package_dir / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks.json").write_text('{"hooks": {}}\n', encoding="ascii")

    view = _derive(root, _lock(locked), modules_root)

    assert len(view.problems) == 1
    assert "manifest not found" in view.problems[0].message


def test_locked_virtual_hook_package_requires_matching_detected_type(
    tmp_path: Path,
) -> None:
    """Hook lock metadata cannot waive a different manifestless package shape."""
    root = _write_manifest(tmp_path, name="root")
    modules_root = tmp_path / "apm_modules"
    locked = LockedDependency(
        repo_url="owner/hooks",
        virtual_path="packages/git-hooks",
        is_virtual=True,
        package_type="hook_package",
        depth=1,
    )
    package_dir = locked.to_dependency_ref().get_install_path(modules_root)
    package_dir.mkdir(parents=True)
    (package_dir / "SKILL.md").write_text("# Not a hook package\n", encoding="ascii")

    view = _derive(root, _lock(locked), modules_root)

    assert len(view.problems) == 1
    assert "manifest not found" in view.problems[0].message


def test_manifestless_virtual_skill_skipped_when_modules_not_materialized(
    tmp_path: Path,
) -> None:
    """`apm audit --ci` without a prior `apm install` leaves apm_modules empty.

    A setup-only CI job installs the CLI then audits, so the package directory
    never exists on disk and the on-disk shape cannot be probed. The frozen
    lockfile classification (`claude_skill`) must waive the missing manifest
    rather than hard-fail, matching the drift check's cold-cache tolerance.
    """
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

    view = _derive(root, _lock(locked), modules_root)

    assert not skill_dir.exists()
    assert view.dependencies == ()
    assert view.problems == ()


def test_manifestless_repo_root_skill_is_skipped(tmp_path: Path) -> None:
    """A materialized repo-root Claude skill ships no apm.yml (#2443)."""
    root = _write_manifest(tmp_path, name="root")
    modules_root = tmp_path / "apm_modules"
    locked = LockedDependency(
        repo_url="owner/skill",
        package_type="claude_skill",
        depth=1,
    )
    skill_dir = locked.to_dependency_ref().get_install_path(modules_root)
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Skill\n", encoding="utf-8")

    view = _derive(root, _lock(locked), modules_root)

    assert not (skill_dir / "apm.yml").exists()
    assert view.dependencies == ()
    assert view.problems == ()


def test_manifestless_repo_root_skill_skipped_when_not_materialized(
    tmp_path: Path,
) -> None:
    """The locked type covers remote repo-root skills on a cold cache."""
    root = _write_manifest(tmp_path, name="root")
    modules_root = tmp_path / "apm_modules"
    locked = LockedDependency(
        repo_url="owner/skill",
        package_type="claude_skill",
        depth=1,
    )
    skill_dir = locked.to_dependency_ref().get_install_path(modules_root)

    view = _derive(root, _lock(locked), modules_root)

    assert not skill_dir.exists()
    assert view.dependencies == ()
    assert view.problems == ()


def test_manifestless_repo_root_without_skill_shape_records_problem(
    tmp_path: Path,
) -> None:
    """A claude_skill lock bit does not waive an invalid installed shape."""
    root = _write_manifest(tmp_path, name="root")
    modules_root = tmp_path / "apm_modules"
    locked = LockedDependency(
        repo_url="owner/skill",
        package_type="claude_skill",
        depth=1,
    )
    locked.to_dependency_ref().get_install_path(modules_root).mkdir(parents=True)

    view = _derive(root, _lock(locked), modules_root)

    assert len(view.problems) == 1
    assert "manifest not found" in view.problems[0].message


def test_manifestless_local_claude_skill_waived(tmp_path: Path) -> None:
    """A local Claude-skill filesystem shape waives its missing manifest.

    A local ``path:`` dependency is resolved directly from the filesystem
    (it can point anywhere, including outside the repo, e.g. ``../sibling``)
    rather than materialised into ``apm_modules/`` -- not a download target,
    so the same defined-by-shape waiver that applies to virtual subdirectory
    packages applies here too, gated by the on-disk shape actually being a
    valid Claude skill (probed below, unlike the cold-cache fallback used
    for virtual packages whose directory may not exist yet).
    """
    root = _write_manifest(tmp_path, name="root")
    skill_dir = tmp_path / "packages" / "skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
    locked = LockedDependency(
        repo_url="_local/skill",
        source="local",
        local_path="./packages/skill",
        package_type="claude_skill",
        depth=1,
    )

    view = _derive(root, _lock(locked), tmp_path / "apm_modules")

    assert view.problems == ()


def test_manifestless_local_package_without_skill_shape_records_problem(
    tmp_path: Path,
) -> None:
    """A local lock bit does not waive an unrecognized on-disk shape."""
    root = _write_manifest(tmp_path, name="root")
    skill_dir = tmp_path / "packages" / "skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "not-a-skill.txt").write_text("nope\n", encoding="utf-8")
    locked = LockedDependency(
        repo_url="_local/skill",
        source="local",
        local_path="./packages/skill",
        package_type="claude_skill",
        depth=1,
    )

    view = _derive(root, _lock(locked), tmp_path / "apm_modules")

    assert len(view.problems) == 1
    assert "manifest not found" in view.problems[0].message


def test_manifestless_missing_local_claude_skill_records_problem(tmp_path: Path) -> None:
    """A missing local directory cannot use the virtual cold-cache waiver."""
    root = _write_manifest(tmp_path, name="root")
    locked = LockedDependency(
        repo_url="_local/missing-skill",
        source="local",
        local_path="./packages/missing-skill",
        package_type="claude_skill",
        depth=1,
    )

    view = _derive(root, _lock(locked), tmp_path / "apm_modules")

    assert len(view.problems) == 1
    assert "manifest not found" in view.problems[0].message


def test_manifestless_virtual_package_without_skill_shape_records_problem(
    tmp_path: Path,
) -> None:
    """A virtual lock bit does not waive an unrecognized installed shape."""
    root = _write_manifest(tmp_path, name="root")
    modules_root = tmp_path / "apm_modules"
    locked = LockedDependency(
        repo_url="angular/skills",
        virtual_path="angular-developer",
        is_virtual=True,
        package_type="claude_skill",
        depth=1,
    )
    locked.to_dependency_ref().get_install_path(modules_root).mkdir(parents=True)

    view = _derive(root, _lock(locked), modules_root)

    assert len(view.problems) == 1
    assert "manifest not found" in view.problems[0].message


def test_manifestless_virtual_package_with_wrong_lock_type_records_problem(
    tmp_path: Path,
) -> None:
    """The manifestless virtual-skill waiver requires matching lock metadata."""
    root = _write_manifest(tmp_path, name="root")
    modules_root = tmp_path / "apm_modules"
    locked = LockedDependency(
        repo_url="angular/skills",
        virtual_path="angular-developer",
        is_virtual=True,
        package_type="apm_package",
        depth=1,
    )
    skill_dir = locked.to_dependency_ref().get_install_path(modules_root)
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Angular Developer\n", encoding="utf-8")

    view = _derive(root, _lock(locked), modules_root)

    assert len(view.problems) == 1
    assert "manifest not found" in view.problems[0].message


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


def test_only_unchanged_previously_trusted_transitive_config_is_preserved(
    tmp_path: Path,
) -> None:
    """A no-op update preserves prior trust without trusting changed code."""
    root = _write_manifest(tmp_path, name="root")
    deep_dir = tmp_path / "packages" / "deep"
    package = _write_manifest(
        deep_dir,
        name="deep",
        mcp=[_self_defined("deep-server", "trusted-command")],
    )
    deep = LockedDependency(
        repo_url="_local/deep",
        source="local",
        local_path="./packages/deep",
        depth=2,
    )
    stored_config = package.get_mcp_dependencies()[0].to_dict()

    preserved = CurrentMcpConfigView.derive(
        root,
        _lock(deep),
        tmp_path / "apm_modules",
        trust_transitive_self_defined=False,
        trusted_transitive_configs={"deep-server": ("deep", stored_config)},
    )
    changed = CurrentMcpConfigView.derive(
        root,
        _lock(deep),
        tmp_path / "apm_modules",
        trust_transitive_self_defined=False,
        trusted_transitive_configs={
            "deep-server": (
                "deep",
                {
                    **stored_config,
                    "command": "different-command",
                },
            )
        },
    )
    wrong_declarer = CurrentMcpConfigView.derive(
        root,
        _lock(deep),
        tmp_path / "apm_modules",
        trust_transitive_self_defined=False,
        trusted_transitive_configs={"deep-server": ("other-package", stored_config)},
    )

    assert [dependency.name for dependency in preserved.dependencies] == ["deep-server"]
    assert changed.dependencies == ()
    assert wrong_declarer.dependencies == ()


def test_view_dependencies_are_mcp_dependency_objects(tmp_path: Path) -> None:
    """The public dependencies tuple remains strongly typed."""
    root = _write_manifest(tmp_path, name="root", mcp=["server"])

    view = _derive(root, None, tmp_path / "apm_modules")

    assert all(isinstance(dep, MCPDependency) for dep in view.dependencies)


def test_dependency_dev_mcp_is_excluded_from_consumer(tmp_path: Path) -> None:
    """Regression #2340: dep devDependencies.mcp must not reach the consumer."""
    root = _write_manifest(tmp_path, name="root")
    dep_dir = tmp_path / "packages" / "dep"
    _write_manifest(
        dep_dir,
        name="dep",
        mcp=[
            {
                "name": "prod-server",
                "registry": False,
                "transport": "http",
                "url": "https://example.com/prod",
            }
        ],
        dev_mcp=[
            {
                "name": "dev-server",
                "registry": False,
                "transport": "http",
                "url": "https://example.com/dev",
            }
        ],
    )
    locked = LockedDependency(
        repo_url="_local/dep",
        source="local",
        local_path="./packages/dep",
        depth=1,
    )

    view = _derive(root, _lock(locked), tmp_path / "apm_modules")

    names = [dep.name for dep in view.dependencies]
    assert "prod-server" in names, "prod MCP from dependency must be included"
    assert "dev-server" not in names, "dev MCP from dependency must NOT be included"


def test_unlocked_compat_excludes_dep_dev_mcp(tmp_path: Path) -> None:
    """Regression #2340: no-lock compat path also excludes dependency dev MCP.

    _collect_unlocked_compat is the legacy path when no lockfile exists;
    it must enforce the same prod-only rule as _collect_locked_dependencies.
    """
    modules_root = tmp_path / "apm_modules"
    dep_dir = modules_root / "dep-pkg"
    _write_manifest(
        dep_dir,
        name="dep-pkg",
        mcp=[
            {
                "name": "prod-server",
                "registry": False,
                "transport": "http",
                "url": "https://example.com/prod",
            }
        ],
        dev_mcp=[
            {
                "name": "dev-server",
                "registry": False,
                "transport": "http",
                "url": "https://example.com/dev",
            }
        ],
    )
    logger = _RecordingLogger()

    # No lock_path -- forces the _collect_unlocked_compat branch.
    result = _collect_transitive_compat(
        modules_root,
        lock_path=None,
        trust_private=True,
        logger=logger,
        diagnostics=None,
    )

    assert [(dependency.name, dependency.resolved_by) for dependency in result] == [
        ("prod-server", "dep-pkg")
    ]
    assert logger.details == [
        "Skipping 1 author-only MCP server(s) from 'dep-pkg'; "
        "transitive devDependencies.mcp do not propagate"
    ]
    assert all("dev-server" not in detail for detail in logger.details)


def test_absent_git_apm_package_dep_is_skipped_on_cold_cache(tmp_path: Path) -> None:
    """Absent non-local apm_package deps emit no problem on cold cache (#2456).

    ``--frozen`` will hydrate these from the lock pins; the missing manifest
    must be treated as benign rather than lockfile drift.
    """
    root = _write_manifest(tmp_path, name="root")
    modules_root = tmp_path / "apm_modules"
    # Remote git apm_package dep -- directory never created (cold cache)
    locked = LockedDependency(
        repo_url="owner/some-pkg",
        resolved_ref="v1.0.0",
        resolved_commit="a" * 40,
        package_type="apm_package",
        depth=1,
    )
    # Confirm the install path does not exist
    assert not locked.to_dependency_ref().get_install_path(modules_root).exists()

    view = _derive(root, _lock(locked), modules_root)

    assert view.problems == (), "absent git apm_package dep must not produce a McpSourceProblem"
    assert view.dependencies == ()


def test_absent_local_apm_package_dep_still_records_problem(tmp_path: Path) -> None:
    """Local apm_package deps with an absent manifest remain an error (#2456).

    Only path-anchored (``source='local'``) packages are excluded from the
    cold-cache exemption; they must exist on disk.
    """
    root = _write_manifest(tmp_path, name="root")
    locked = LockedDependency(
        repo_url="_local/missing-pkg",
        source="local",
        local_path="./packages/missing-pkg",
        package_type="apm_package",
        depth=1,
    )

    view = _derive(root, _lock(locked), tmp_path / "apm_modules")

    assert len(view.problems) == 1
    assert "manifest not found" in view.problems[0].message


@pytest.mark.parametrize(
    ("package_type", "expected_kind"),
    (
        ("apm_package", "APM package"),
        ("claude_skill", "Claude skill"),
        ("agent_plugin", "Agent plugin"),
    ),
)
def test_cold_cache_exemption_emits_verbose_trace(
    tmp_path: Path,
    package_type: str,
    expected_kind: str,
) -> None:
    """Each remote cold-cache waiver explains its locked type in verbose mode."""
    root = _write_manifest(tmp_path, name="root")
    modules_root = tmp_path / "apm_modules"
    locked = LockedDependency(
        repo_url="owner/some-pkg",
        resolved_ref="v1.0.0",
        resolved_commit="a" * 40,
        package_type=package_type,
        depth=1,
        name="some-pkg",
    )
    assert not locked.to_dependency_ref().get_install_path(modules_root).exists()

    logger = _RecordingLogger()
    view = CurrentMcpConfigView.derive(
        root,
        _lock(locked),
        modules_root,
        trust_transitive_self_defined=True,
        logger=logger,
    )

    assert view.problems == ()
    cold_cache_details = [d for d in logger.details if "cold cache" in d.lower()]
    assert len(cold_cache_details) == 1
    assert "some-pkg" in cold_cache_details[0]
    assert expected_kind in cold_cache_details[0]


@pytest.mark.parametrize("native_kind", ["agent_plugin", "marketplace_plugin"])
@pytest.mark.parametrize("locked_type", ["agent_plugin", "apm_package", "marketplace_plugin"])
def test_manifestless_native_plugin_requires_matching_locked_type(
    tmp_path: Path, locked_type: str, native_kind: str
) -> None:
    """The current MCP view waives only a valid, matching native package."""
    root = _write_manifest(tmp_path, name="root")
    modules = tmp_path / "apm_modules"
    locked = LockedDependency(
        repo_url="owner/native-plugin",
        resolved_ref="v1",
        resolved_commit="a" * 40,
        package_type=locked_type,
        depth=1,
    )
    package_dir = locked.to_dependency_ref().get_install_path(modules)
    if native_kind == "agent_plugin":
        write_agent_plugin(package_dir, name="native-plugin", with_mcp=False)
    else:
        manifest = package_dir / ".codex-plugin/plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(
            json.dumps(
                {
                    "name": "native-plugin",
                    "mcpServers": {"native-only": {"command": "DO_NOT_EXECUTE"}},
                }
            ),
            encoding="utf-8",
        )
    before = {path: path.read_bytes() for path in package_dir.rglob("*") if path.is_file()}

    view = _derive(root, _lock(locked), modules)

    assert len(view.problems) == (0 if locked_type == native_kind else 1)
    assert view.dependencies == ()
    assert not (package_dir / "apm.yml").exists()
    assert before == {path: path.read_bytes() for path in package_dir.rglob("*") if path.is_file()}
