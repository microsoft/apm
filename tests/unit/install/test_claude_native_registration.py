"""Settings-only native Claude declaration owned by apm.lock.yaml (#3191, #3181).

Lifecycle detail beyond the req-na conformance tests in
tests/spec_conformance/test_native_activation_profile_reqs.py.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apm_cli.agent_plugins.errors import AgentPluginDeploymentBoundaryError
from apm_cli.claude_plugins.admission import (
    ClaudeNativePlugin,
    ClaudeNativePluginError,
    fingerprint_native_claude_source,
    pin_native_claude_source,
)
from apm_cli.claude_plugins.registrar import (
    marketplace_binding,
    marketplace_name,
    native_ownership,
    reconcile_claude_plugins,
    verify_claude_native_cache,
)
from apm_cli.core.scope import InstallScope
from apm_cli.deps.lockfile import LockedDependency, LockFile, LockfileFormatError
from apm_cli.native_plugins.record import NativePluginRecord
from apm_cli.native_plugins.settings import (
    BINDING_CONFLICT,
    NativeSettingsError,
    SettingsDeclaration,
    write_declaration,
)

_NAME = "apm-u-3f9c"


def test_unparseable_settings_fail_closed_without_a_write(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text("{broken", encoding="utf-8")
    declaration = SettingsDeclaration(_NAME, tmp_path, frozenset({"a"}), frozenset())
    with pytest.raises(NativeSettingsError):
        write_declaration(settings, declaration)
    assert settings.read_text(encoding="utf-8") == "{broken"


def test_symlinked_settings_are_written_through_to_their_target(tmp_path: Path) -> None:
    real = tmp_path / "dotfiles/settings.json"
    real.parent.mkdir()
    real.write_text('{"theme": "dark"}', encoding="utf-8")
    link = tmp_path / "settings.json"
    link.symlink_to(real)
    write_declaration(link, SettingsDeclaration(_NAME, tmp_path, frozenset({"a"}), frozenset()))
    assert link.is_symlink()
    assert json.loads(real.read_text())["enabledPlugins"] == {f"a@{_NAME}": True}


def test_marketplace_name_is_deterministic_and_unique_per_apm_home(tmp_path: Path) -> None:
    first, second = tmp_path / "one/.apm", tmp_path / "two/.apm"
    assert marketplace_name(first) == marketplace_name(first)
    assert marketplace_name(first) != marketplace_name(second)
    assert marketplace_name(first).startswith("apm-u-")


def test_malformed_native_record_fails_lock_parsing() -> None:
    data = LockedDependency(repo_url="owner/plugin").to_dict()
    data["native_plugin"] = {
        "name": "alpha",
        "root": "../escape",
        "fingerprint": "sha256:" + "0" * 64,
    }
    with pytest.raises(LockfileFormatError, match="native_plugin"):
        LockedDependency.from_dict(data)


class _Home:
    """An isolated user home with apm_modules, a lock and Claude settings."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.home = root / "home"
        self.modules = self.home / ".apm/apm_modules"
        self.settings = self.home / ".claude/settings.json"
        self.plugins = self.home / ".agents/plugins"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(self.home / ".claude"))
        self.lock = LockFile()
        self.marketplace = marketplace_name(self.modules.parent)

    def source(self, repo: str, name: str, body: str = "v1", commit: str = "a") -> None:
        root = self.modules / repo
        (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        (root / ".claude-plugin/plugin.json").write_text(json.dumps({"name": name}))
        (root / "commands").mkdir(exist_ok=True)
        (root / "commands/status.md").write_text(body)
        (root / ".git").mkdir(exist_ok=True)
        (root / ".git/HEAD").write_text("ref")
        dependency = LockedDependency(
            repo_url=repo, resolved_commit=commit * 40, package_type="marketplace_plugin"
        )
        dependency.native_plugin = NativePluginRecord(
            name,
            fingerprint_native_claude_source(root),
            *marketplace_binding(self.modules.parent, self.home),
        )
        self.lock.dependencies[repo] = dependency

    def reconcile(self, previous, prepared=None) -> None:
        reconcile_claude_plugins(
            modules_dir=self.modules,
            scope=InstallScope.USER,
            previous=previous,
            lockfile=self.lock,
            prepared=prepared,
            deploy_root=self.home,
        )

    def prepared(self, repo: str, approved_pin: str) -> dict[str, ClaudeNativePlugin]:
        record = self.lock.dependencies[repo].native_plugin
        root = self.modules / repo
        return {
            repo: ClaudeNativePlugin(
                repo, record.name, root, record.fingerprint, approved_pin=approved_pin
            )
        }

    def declared(self) -> dict:
        return json.loads(self.settings.read_text()) if self.settings.exists() else {}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Home:
    return _Home(tmp_path, monkeypatch)


def test_first_install_deploys_the_approved_root_and_declares_only_two_keys(home) -> None:
    home.source("owner/alpha", "alpha")
    home.reconcile(None)
    deployed = home.plugins / "alpha"
    assert (deployed / "commands/status.md").read_text() == "v1"
    assert not (deployed / ".git").exists()
    record = home.lock.dependencies["owner/alpha"].native_plugin
    assert fingerprint_native_claude_source(deployed) == record.fingerprint
    catalog = json.loads((home.plugins / ".claude-plugin/marketplace.json").read_text())
    assert catalog["plugins"] == [{"name": "alpha", "source": "./alpha"}]
    assert home.declared() == {
        "extraKnownMarketplaces": {
            home.marketplace: {"source": {"source": "directory", "path": str(home.plugins)}}
        },
        "enabledPlugins": {f"alpha@{home.marketplace}": True},
    }


def test_same_pin_content_change_is_refused(home) -> None:
    home.source("owner/alpha", "alpha")
    home.reconcile(None)
    previous = native_ownership(home.lock)
    home.source("owner/alpha", "alpha", body="tampered at the same commit")
    with pytest.raises(ClaudeNativePluginError, match="same pinned revision"):
        home.reconcile(previous)
    assert (home.plugins / "alpha/commands/status.md").read_text() == "v1"


def test_a_new_pin_updates_the_deployed_copy(home) -> None:
    home.source("owner/alpha", "alpha")
    home.reconcile(None)
    previous = native_ownership(home.lock)
    home.source("owner/alpha", "alpha", body="v2", commit="b")
    home.reconcile(previous)
    assert (home.plugins / "alpha/commands/status.md").read_text() == "v2"


def test_unrecorded_existing_root_is_refused_and_settings_unchanged(home) -> None:
    home.source("owner/alpha", "alpha")
    foreign = home.plugins / "alpha"
    foreign.mkdir(parents=True)
    (foreign / "mine.md").write_text("user content")
    with pytest.raises(ClaudeNativePluginError, match="does not record it"):
        home.reconcile(None)
    assert (foreign / "mine.md").read_text() == "user content"
    assert not home.settings.exists()


def test_removal_retires_only_what_the_previous_lock_produced(home) -> None:
    home.source("owner/alpha", "alpha")
    home.source("owner/beta", "beta")
    home.reconcile(None)
    previous = native_ownership(home.lock)
    edited = home.plugins / "beta/commands/status.md"
    edited.write_text("edited by the user")
    del home.lock.dependencies["owner/alpha"]
    del home.lock.dependencies["owner/beta"]
    home.reconcile(previous)
    assert not (home.plugins / "alpha").exists()
    # A deployed root that changed since APM wrote it is kept, but undeclared.
    assert edited.read_text() == "edited by the user"
    assert home.declared() == {"extraKnownMarketplaces": {}, "enabledPlugins": {}}


def test_binding_conflict_refuses_and_rolls_back_new_roots(home) -> None:
    home.source("owner/alpha", "alpha")
    home.settings.parent.mkdir(parents=True)
    foreign = {
        "extraKnownMarketplaces": {
            home.marketplace: {"source": {"source": "directory", "path": "/elsewhere"}}
        }
    }
    home.settings.write_text(json.dumps(foreign))
    with pytest.raises(ClaudeNativePluginError, match="another source"):
        home.reconcile(None)
    assert json.loads(home.settings.read_text()) == foreign
    assert not home.plugins.exists()


def test_failed_settings_commit_restores_replaced_roots_and_settings_bytes(
    home, monkeypatch: pytest.MonkeyPatch
) -> None:
    home.source("owner/alpha", "alpha")
    home.reconcile(None)
    previous = native_ownership(home.lock)
    deployed_fingerprint = previous["owner/alpha"].record.fingerprint
    before = home.settings.read_bytes()
    home.source("owner/alpha", "alpha", body="v2", commit="b")
    home.source("owner/beta", "beta")
    from apm_cli.native_plugins import reconcile as owner

    real_write = owner.write_declaration

    def conflicting_write(path, declaration):
        change = real_write(path, declaration)
        change.diagnostics.append(BINDING_CONFLICT)
        return change

    monkeypatch.setattr(owner, "write_declaration", conflicting_write)
    with pytest.raises(ClaudeNativePluginError, match="another source"):
        home.reconcile(previous)
    assert fingerprint_native_claude_source(home.plugins / "alpha") == deployed_fingerprint
    assert not (home.plugins / "beta").exists()
    assert home.settings.read_bytes() == before
    assert sorted(path.name for path in home.plugins.iterdir()) == [".claude-plugin", "alpha"]


def test_unreadable_retiring_root_is_kept_without_undoing_the_commit(
    home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control = _Home(tmp_path / "control", monkeypatch)
    for each in (control, home):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(each.home / ".claude"))
        each.source("owner/alpha", "alpha")
        each.reconcile(None)
    previous = {each: native_ownership(each.lock) for each in (control, home)}
    planted = home.plugins / "alpha/commands/link.md"
    planted.symlink_to(home.plugins / "alpha/commands/status.md")
    for each in (control, home):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(each.home / ".claude"))
        del each.lock.dependencies["owner/alpha"]
        each.reconcile(previous[each])
    assert planted.is_symlink()
    assert not (control.plugins / "alpha").exists()
    assert home.settings.read_bytes() == control.settings.read_bytes()
    assert home.declared() == {"extraKnownMarketplaces": {}, "enabledPlugins": {}}


def test_deploy_refuses_a_copy_that_differs_from_the_approved_pin(home) -> None:
    home.source("owner/alpha", "alpha")
    with pytest.raises(ClaudeNativePluginError, match="approved with 'apm approve'"):
        home.reconcile(None, home.prepared("owner/alpha", "sha256:" + "0" * 64))
    assert not home.plugins.exists()
    assert not home.settings.exists()
    pin = pin_native_claude_source(home.modules / "owner/alpha")
    home.reconcile(None, home.prepared("owner/alpha", pin))
    assert (home.plugins / "alpha/commands/status.md").read_text() == "v1"


def test_audit_verifies_source_and_deployed_copy_against_the_lock(home) -> None:
    home.source("owner/alpha", "alpha")
    home.reconcile(None)
    locked = home.lock.dependencies["owner/alpha"]
    source = home.modules / "owner/alpha"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("HOME", str(home.home))
        patch.setenv("USERPROFILE", str(home.home))
        verify_claude_native_cache(modules_dir=home.modules, locked=locked, source_root=source)
        script = home.plugins / "alpha/commands/status.md"
        if os.name != "nt":
            script.chmod(0o755)
            with pytest.raises(AgentPluginDeploymentBoundaryError, match="deployed copy differs"):
                verify_claude_native_cache(
                    modules_dir=home.modules, locked=locked, source_root=source
                )
