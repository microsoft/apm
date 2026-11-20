"""Project-scope native plugins for Claude and Copilot (#3921).

The committed ``.agents/plugins`` root, the relative ``./.agents/plugins``
marketplace, per-project names, per-harness settings files chosen by the
entry's targets, lock-owned removal, and Copilot's file precedence.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.claude_plugins.registrar import reconcile_claude_plugins
from apm_cli.core.scope import InstallScope
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.native_plugins.binding import (
    PROJECT_MARKETPLACE_PATH,
    compute_native_plugin_portable_hash,
    project_binding,
    project_identity,
    project_marketplace_name,
)
from apm_cli.native_plugins.errors import NativePluginError
from apm_cli.native_plugins.harnesses import (
    COPILOT_PROJECT_READ_ORDER,
    effective_enable,
    entry_harnesses,
    read_order,
)
from apm_cli.native_plugins.reconcile import native_ownership
from apm_cli.native_plugins.record import NativePluginRecord

pytestmark = pytest.mark.component

_CLAUDE = ".claude/settings.json"
_COPILOT = ".github/copilot/settings.json"


def test_portable_digest_byte_layout_is_the_canonical_pin(tmp_path: Path) -> None:
    """Swap guard for #3989: path, NUL, raw sha256(bytes); no mode byte."""
    root = tmp_path / "plugin"
    (root / "hooks").mkdir(parents=True)
    (root / "hooks/run.sh").write_bytes(b"#!/bin/sh\r\necho hi\n")
    (root / "a.md").write_bytes(b"alpha")
    (root / "apm.yml").write_text("name: synthesized")
    expected = hashlib.sha256()
    for relative, data in (("a.md", b"alpha"), ("hooks/run.sh", b"#!/bin/sh\r\necho hi\n")):
        expected.update(relative.encode() + b"\0" + hashlib.sha256(data).digest())
    digest = compute_native_plugin_portable_hash(root)
    assert digest == f"sha256:{expected.hexdigest()}"
    if os.name != "nt":
        (root / "hooks/run.sh").chmod(0o755)
        assert compute_native_plugin_portable_hash(root) == digest


def test_project_marketplace_name_follows_the_project_identity(tmp_path: Path) -> None:
    one, two = tmp_path / "clone-a", tmp_path / "clone-b"
    for root in (one, two):
        root.mkdir()
        (root / "apm.yml").write_text("name: consumer\nversion: 1.0.0\n")
    # Every clone, whatever its path, declares the same per-project name.
    assert project_binding(one).marketplace == project_binding(two).marketplace
    assert project_binding(one).path == PROJECT_MARKETPLACE_PATH == "./.agents/plugins"
    assert project_marketplace_name("consumer").startswith("apm-p-")
    assert project_marketplace_name("consumer") != project_marketplace_name("other")


def test_project_without_a_name_cannot_name_its_marketplace(tmp_path: Path) -> None:
    (tmp_path / "apm.yml").write_text("version: 1.0.0\n")
    with pytest.raises(NativePluginError, match="'name'"):
        project_identity(tmp_path)


@pytest.mark.parametrize(
    ("package_type", "subset", "configured", "expected"),
    [
        ("marketplace_plugin", [], {"claude", "copilot", "codex"}, {"claude", "copilot"}),
        ("marketplace_plugin", ["copilot"], {"claude", "copilot"}, {"copilot"}),
        ("marketplace_plugin", [], {"codex"}, set()),
        ("agent_plugin", [], {"claude"}, {"copilot"}),
    ],
)
def test_entry_harnesses_follow_the_entry_targets(package_type, subset, configured, expected):
    locked = SimpleNamespace(package_type=package_type, target_subset=subset)
    assert entry_harnesses(locked, project=True, configured=configured) == expected
    assert entry_harnesses(locked, project=False, configured=()) == (
        {"copilot"} if package_type == "agent_plugin" else {"claude"}
    )


@pytest.mark.parametrize(
    ("claude", "copilot", "effective", "winner"),
    [
        (True, False, False, _COPILOT),
        (False, True, True, _COPILOT),
        (False, None, False, _CLAUDE),
    ],
)
def test_copilot_file_precedence_pins_github_over_claude(
    tmp_path: Path, claude, copilot, effective, winner
) -> None:
    """When both .claude/ and .github/copilot/ name the plugin, Copilot's own file wins.

    Observed on Copilot CLI 1.0.112; the gated e2e test re-checks a live CLI.
    """
    key = "demo@apm-p-test"
    for relative, value in ((_CLAUDE, claude), (_COPILOT, copilot)):
        if value is not None:
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"enabledPlugins": {key: value}}))
    files = read_order("copilot", deploy_root=tmp_path, project=True)
    assert [f.relative_to(tmp_path).as_posix() for f in files] == list(COPILOT_PROJECT_READ_ORDER)
    value, source = effective_enable(files, key)
    assert value is effective
    assert source.relative_to(tmp_path).as_posix() == winner


class _Project:
    """A project with apm_modules, a lock and both harness settings files."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.modules = root / "apm_modules"
        root.mkdir(parents=True)
        (root / "apm.yml").write_text("name: consumer\nversion: 1.0.0\n")
        self.binding = project_binding(root)
        self.lock = LockFile()

    def source(self, repo: str, name: str, *, targets: list[str] | None = None) -> None:
        src = self.modules / repo
        (src / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        (src / ".claude-plugin/plugin.json").write_text(json.dumps({"name": name}))
        (src / "skills/hi").mkdir(parents=True, exist_ok=True)
        (src / "skills/hi/SKILL.md").write_text("hi")
        dependency = LockedDependency(
            repo_url=repo,
            resolved_commit="a" * 40,
            package_type="marketplace_plugin",
            target_subset=targets or [],
        )
        dependency.native_plugin = NativePluginRecord(
            name, compute_native_plugin_portable_hash(src), *self.binding.pair
        )
        self.lock.dependencies[repo] = dependency

    def reconcile(self, previous, configured=("claude", "copilot")) -> None:
        reconcile_claude_plugins(
            modules_dir=self.modules,
            scope=InstallScope.PROJECT,
            previous=previous,
            lockfile=self.lock,
            deploy_root=self.root,
            configured=configured,
        )

    def settings(self, relative: str) -> dict:
        path = self.root / relative
        return json.loads(path.read_text()) if path.exists() else {}

    def write(self, relative: str, document: dict) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document))


@pytest.fixture
def project(tmp_path: Path) -> _Project:
    return _Project(tmp_path / "repo")


def test_install_commits_root_catalog_and_relative_settings(project: _Project) -> None:
    project.source("owner/alpha", "alpha")
    project.reconcile(None)
    name = project.binding.marketplace
    assert (project.root / ".agents/plugins/alpha/skills/hi/SKILL.md").is_file()
    catalog_path = project.root / ".agents/plugins/.claude-plugin/marketplace.json"
    assert json.loads(catalog_path.read_text()) == {
        "name": name,
        "owner": {"name": "APM"},
        "plugins": [{"name": "alpha", "source": "./alpha"}],
    }
    expected = {
        "extraKnownMarketplaces": {
            name: {"source": {"source": "directory", "path": "./.agents/plugins"}}
        },
        "enabledPlugins": {f"alpha@{name}": True},
    }
    assert project.settings(_CLAUDE) == expected
    assert project.settings(_COPILOT) == expected


def test_each_harness_file_follows_the_entry_targets(project: _Project) -> None:
    project.source("owner/alpha", "alpha", targets=["copilot"])
    project.reconcile(None)
    assert not (project.root / _CLAUDE).exists()
    assert project.settings(_COPILOT)["enabledPlugins"] == {
        f"alpha@{project.binding.marketplace}": True
    }
    # A project that stops targeting Copilot retires only the entries APM owned.
    previous = native_ownership(project.lock)
    project.reconcile(previous, configured=("claude",))
    assert project.settings(_COPILOT) == {"extraKnownMarketplaces": {}, "enabledPlugins": {}}


def test_uninstall_removes_only_apm_owned_entries(project: _Project) -> None:
    name = project.binding.marketplace
    project.write(_CLAUDE, {"theme": "dark", "enabledPlugins": {"mine@other": True}})
    project.source("owner/alpha", "alpha")
    project.source("owner/beta", "beta")
    project.reconcile(None)
    claude = project.settings(_CLAUDE)
    claude["enabledPlugins"][f"beta@{name}"] = False  # the user's explicit choice
    project.write(_CLAUDE, claude)
    previous = native_ownership(project.lock)
    project.lock.dependencies.clear()
    project.reconcile(previous)
    assert project.settings(_CLAUDE) == {
        "theme": "dark",
        "extraKnownMarketplaces": {},
        "enabledPlugins": {"mine@other": True, f"beta@{name}": False},
    }
    assert project.settings(_COPILOT) == {"extraKnownMarketplaces": {}, "enabledPlugins": {}}
    assert not (project.root / ".agents/plugins").exists()


def test_foreign_binding_in_a_file_copilot_reads_refuses_before_any_write(
    project: _Project,
) -> None:
    project.write(
        ".claude/settings.local.json",
        {
            "extraKnownMarketplaces": {
                project.binding.marketplace: {"source": {"source": "directory", "path": "/x"}}
            }
        },
    )
    project.source("owner/alpha", "alpha")
    with pytest.raises(NativePluginError, match=r"settings\.local\.json"):
        project.reconcile(None)
    assert not (project.root / _CLAUDE).exists()
    assert not (project.root / _COPILOT).exists()
    assert not (project.root / ".agents/plugins").exists()


def test_failed_second_settings_write_restores_the_first(
    project: _Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apm_cli.native_plugins import reconcile as module

    project.write(_CLAUDE, {"theme": "dark"})
    original = module.write_declaration

    def fail_on_copilot(path, declaration):
        if path.as_posix().endswith(_COPILOT):
            raise OSError("disk full")
        return original(path, declaration)

    monkeypatch.setattr(module, "write_declaration", fail_on_copilot)
    project.source("owner/alpha", "alpha")
    with pytest.raises(NativePluginError, match="disk full"):
        project.reconcile(None)
    assert project.settings(_CLAUDE) == {"theme": "dark"}
    assert not (project.root / ".agents/plugins").exists()


def test_direct_claimant_wins_and_same_precedence_fails() -> None:
    """req-tg-013 claimant selection, case-folded, whatever the plugin format."""
    from apm_cli.native_plugins.claimants import Claimant, select_claimants

    winners, notes = select_claimants(
        {"owner/direct": Claimant("Demo", True), "owner/transitive": Claimant("demo", False)}, {}
    )
    assert winners == {"owner/direct"}
    assert "skipping owner/transitive" in notes[0]
    with pytest.raises(NativePluginError, match="same precedence"):
        select_claimants({"a/one": Claimant("demo", False), "b/two": Claimant("demo", False)}, {})


def test_transitive_claimant_never_silently_repoints_a_prior_owner() -> None:
    from apm_cli.native_plugins.claimants import Claimant, select_claimants

    prior = {"demo": "owner/old"}
    winners, notes = select_claimants({"owner/new": Claimant("demo", False)}, prior)
    assert winners == frozenset()
    assert "not repointed" in notes[0]
    winners, _ = select_claimants({"owner/new": Claimant("demo", True)}, prior)
    assert winners == {"owner/new"}
