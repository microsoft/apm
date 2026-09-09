"""Native Agent Plugins v1 lifecycle-registration conformance -- sec.8.5.7.

req-tg-013 is the Mode C amendment that records a qualified,
machine-verifiable consumer lifecycle. These tests drive the real
registration owner in ``src/apm_cli/copilot_plugins/`` and assert the
observable guarantees the requirement encodes:

* one aggregate registration per scope covering direct + transitive
  Agent Plugin dependencies;
* the registration references the materialized package in place and
  never copies it into a private plugin store;
* ledger-primary ownership supports exact-entry recovery of APM's reserved
  namespace, refuses foreign collisions, and preserves unrelated settings
  values semantically even when stable serialization reformats the document;
* target-native registration applies only when the effective targets include
  ``copilot``; other targets remain behind the req-tg-011 boundary.

The suite is hermetic: capability resolution is a pure target-selection
decision and never discovers or executes a Copilot runtime.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.agent_plugins.errors import AgentPluginDeploymentBoundaryError
from apm_cli.copilot_plugins.capability import (
    native_registration_scope,
    resolve_native_registration_capability,
)
from apm_cli.copilot_plugins.constants import (
    ENABLED_PLUGINS_KEY,
    EXTRA_MARKETPLACES_KEY,
)
from apm_cli.copilot_plugins.registrar import (
    ResolvedPluginCandidate,
    catalog_path_for,
    ledger_path_for,
    synchronize_copilot_plugins,
)
from apm_cli.copilot_plugins.settings import (
    CopilotSettingsCollisionError,
    read_ledger,
)
from apm_cli.core.scope import InstallScope
from tests.spec_conformance._helpers import assert_spec_contains
from tests.unit.copilot_plugins._builders import read_json, write_agent_plugin

_COPILOT_TARGET = SimpleNamespace(name="copilot")


def _project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "project"
    modules = project / "apm_modules"
    modules.mkdir(parents=True)
    return project, modules


def _settings_path(project: Path) -> Path:
    return project / ".github" / "copilot" / "settings.local.json"


def _copilot_capability():
    """Resolve the real capability on the Copilot target path."""
    capability = resolve_native_registration_capability([_COPILOT_TARGET])
    assert capability.supported is True
    return capability


@pytest.mark.req("req-tg-013")
def test_native_registration_is_aggregate_in_place_and_consumer_owned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One in-place aggregate registration, proven by consumer-owned state."""
    project, modules = _project(tmp_path)
    direct = modules / "acme" / "direct"
    transitive = modules / "acme" / "nested" / "transitive"
    write_agent_plugin(direct, name="direct-plugin")
    write_agent_plugin(transitive, name="transitive-plugin")

    capability = _copilot_capability()
    result = synchronize_copilot_plugins(
        project_root=project,
        modules_dir=modules,
        scope=InstallScope.PROJECT,
        candidates=[
            ResolvedPluginCandidate("acme/direct", direct),
            ResolvedPluginCandidate("acme/nested/transitive", transitive),
        ],
        capability=capability,
    )

    # One aggregate catalog per scope covers direct AND transitive deps.
    catalog_path = catalog_path_for(modules)
    assert catalog_path == modules / ".github" / "plugin" / "marketplace.json"
    catalog = read_json(catalog_path)
    assert result.plugin_names == ("direct-plugin", "transitive-plugin")
    sources = [entry["source"] for entry in catalog["plugins"]]
    assert sources == ["./acme/direct", "./acme/nested/transitive"]

    # Reference in place: every source resolves to the real materialized
    # package under apm_modules; nothing is copied into a private store.
    for source, expected in zip(sources, (direct, transitive), strict=True):
        assert source.startswith("./")
        target = (modules / source[2:]).resolve()
        assert target == expected.resolve()
        assert modules.resolve() in target.parents
        assert (target / "plugin.json").is_file()

    # The activation surface points at the materialized root in place.
    settings = read_json(_settings_path(project))
    assert settings[EXTRA_MARKETPLACES_KEY]["apm"]["source"] == {
        "source": "directory",
        "path": "apm_modules",
    }

    # Ownership lives in consumer-owned state, not a guessed value shape.
    ledger = read_ledger(ledger_path_for(modules))
    assert ledger.marketplace_owned is True
    assert ledger.enabled_plugins == (
        "direct-plugin@apm",
        "transitive-plugin@apm",
    )


@pytest.mark.req("req-tg-013")
def test_removal_is_exact_and_unowned_entries_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removal retires only owned rows; a foreign entry is never overwritten."""
    project, modules = _project(tmp_path)
    write_agent_plugin(modules / "pkg", name="portable-plugin")
    candidate = ResolvedPluginCandidate("pkg", modules / "pkg")

    settings_path = _settings_path(project)
    settings_path.parent.mkdir(parents=True)
    original = (
        '{"banner":"keep me","extraKnownMarketplaces":{"team":{"source":'
        '{"source":"directory","path":"x"}}},"enabledPlugins":'
        '{"team-plugin@team":true}}'
    )
    settings_path.write_text(original, encoding="ascii")

    capability = _copilot_capability()
    synchronize_copilot_plugins(
        project_root=project,
        modules_dir=modules,
        scope=InstallScope.PROJECT,
        candidates=[candidate],
        capability=capability,
    )
    after_register = read_json(settings_path)
    assert after_register[ENABLED_PLUGINS_KEY]["portable-plugin@apm"] is True
    assert after_register["banner"] == "keep me"
    assert settings_path.read_text(encoding="ascii") != original

    # An empty resolved set (uninstall/prune) retires exactly APM's rows.
    synchronize_copilot_plugins(
        project_root=project,
        modules_dir=modules,
        scope=InstallScope.PROJECT,
        candidates=[],
        capability=capability,
    )
    after_remove = read_json(settings_path)
    assert "portable-plugin@apm" not in after_remove.get(ENABLED_PLUGINS_KEY, {})
    assert "apm" not in after_remove.get(EXTRA_MARKETPLACES_KEY, {})
    assert not catalog_path_for(modules).exists()
    assert not ledger_path_for(modules).exists()
    # Every unrelated key and value survives semantically.
    assert after_remove["banner"] == "keep me"
    assert after_remove[EXTRA_MARKETPLACES_KEY]["team"] == {
        "source": {"source": "directory", "path": "x"}
    }
    assert after_remove[ENABLED_PLUGINS_KEY]["team-plugin@team"] is True

    # A namespaced entry APM does not own is refused, not overwritten.
    foreign = json.dumps(
        {EXTRA_MARKETPLACES_KEY: {"apm": {"source": {"source": "git", "path": "x"}}}},
        indent=2,
    )
    settings_path.write_text(foreign, encoding="ascii")
    with pytest.raises(CopilotSettingsCollisionError, match=r"does not own it"):
        synchronize_copilot_plugins(
            project_root=project,
            modules_dir=modules,
            scope=InstallScope.PROJECT,
            candidates=[candidate],
            capability=capability,
        )
    assert settings_path.read_text(encoding="ascii") == foreign
    assert not catalog_path_for(modules).exists()


@pytest.mark.req("req-tg-013")
def test_non_copilot_target_creates_no_native_registration(tmp_path: Path) -> None:
    """A target set without Copilot creates no target registration state."""
    project, modules = _project(tmp_path)
    write_agent_plugin(modules / "pkg", name="portable-plugin")

    capability = resolve_native_registration_capability([SimpleNamespace(name="claude")])
    assert capability.supported is False
    with pytest.raises(AgentPluginDeploymentBoundaryError):
        capability.require()

    # Admission is the gate that lets native registration replace primitive
    # projection; a non-Copilot target admits nothing, so the boundary owns it.
    plugin_info = SimpleNamespace(
        package_type=None,
        package=SimpleNamespace(agent_plugin=object()),
    )
    from apm_cli.copilot_plugins.capability import admits_native_plugin
    from apm_cli.models.validation import PackageType

    plugin_info.package_type = PackageType.AGENT_PLUGIN
    with native_registration_scope([SimpleNamespace(name="claude")]):
        assert admits_native_plugin(plugin_info) is False

    # A non-applicable capability with no prior registration writes nothing.
    result = synchronize_copilot_plugins(
        project_root=project,
        modules_dir=modules,
        scope=InstallScope.PROJECT,
        candidates=[ResolvedPluginCandidate("pkg", modules / "pkg")],
        capability=capability,
    )
    assert result.changed is False
    assert not catalog_path_for(modules).exists()
    assert not ledger_path_for(modules).exists()
    assert not _settings_path(project).exists()


@pytest.mark.req("req-tg-013")
def test_missing_ledger_re_adopts_only_the_exact_reserved_namespace(tmp_path: Path) -> None:
    """Exact APM state is recoverable; manual @apm keys are not supported."""
    project, modules = _project(tmp_path)
    write_agent_plugin(modules / "pkg", name="portable-plugin")
    candidate = ResolvedPluginCandidate("pkg", modules / "pkg")
    capability = _copilot_capability()
    synchronize_copilot_plugins(
        project_root=project,
        modules_dir=modules,
        scope=InstallScope.PROJECT,
        candidates=[candidate],
        capability=capability,
    )
    ledger_path_for(modules).unlink()
    settings_path = _settings_path(project)
    settings = read_json(settings_path)
    settings[ENABLED_PLUGINS_KEY]["manual@apm"] = True
    settings[ENABLED_PLUGINS_KEY]["team@other"] = True
    settings_path.write_text(json.dumps(settings), encoding="ascii")

    synchronize_copilot_plugins(
        project_root=project,
        modules_dir=modules,
        scope=InstallScope.PROJECT,
        candidates=[candidate],
        capability=capability,
    )

    recovered = read_json(settings_path)
    assert recovered[ENABLED_PLUGINS_KEY] == {
        "portable-plugin@apm": True,
        "team@other": True,
    }
    assert read_ledger(ledger_path_for(modules)).marketplace_owned is True


@pytest.mark.req("req-tg-013")
def test_invalid_settings_fail_closed_without_overwrite(tmp_path: Path) -> None:
    """Invalid JSON/JSONC is never overwritten by registration."""
    project, modules = _project(tmp_path)
    write_agent_plugin(modules / "pkg", name="portable-plugin")
    settings_path = _settings_path(project)
    settings_path.parent.mkdir(parents=True)
    original = '{\n  // keep this comment\n  "enabledPlugins": {}\n}\n'
    settings_path.write_text(original, encoding="ascii")

    with pytest.raises(CopilotSettingsCollisionError, match=r"is not valid JSON"):
        synchronize_copilot_plugins(
            project_root=project,
            modules_dir=modules,
            scope=InstallScope.PROJECT,
            candidates=[ResolvedPluginCandidate("pkg", modules / "pkg")],
            capability=_copilot_capability(),
        )

    assert settings_path.read_text(encoding="ascii") == original
    assert not catalog_path_for(modules).exists()
    assert not ledger_path_for(modules).exists()


@pytest.mark.req("req-tg-013")
def test_plugin_name_precedence_and_owner_identity_are_deterministic(
    tmp_path: Path,
) -> None:
    """Direct wins; ambiguous peers and transitive ownership flips refuse."""
    direct_project, direct_modules = _project(tmp_path / "direct-wins")
    direct = direct_modules / "direct"
    transitive = direct_modules / "transitive"
    write_agent_plugin(direct, name="shared-plugin")
    write_agent_plugin(transitive, name="shared-plugin")
    direct_result = synchronize_copilot_plugins(
        project_root=direct_project,
        modules_dir=direct_modules,
        scope=InstallScope.PROJECT,
        candidates=[
            ResolvedPluginCandidate("transitive", transitive, direct=False),
            ResolvedPluginCandidate("direct", direct, direct=True),
        ],
        capability=_copilot_capability(),
    )
    assert direct_result.plugin_names == ("shared-plugin",)
    assert read_json(catalog_path_for(direct_modules))["plugins"][0]["source"] == "./direct"

    peer_project, peer_modules = _project(tmp_path / "peer-collision")
    first_peer = peer_modules / "first"
    second_peer = peer_modules / "second"
    write_agent_plugin(first_peer, name="shared-plugin")
    write_agent_plugin(second_peer, name="shared-plugin")
    with pytest.raises(CopilotSettingsCollisionError, match=r"same precedence"):
        synchronize_copilot_plugins(
            project_root=peer_project,
            modules_dir=peer_modules,
            scope=InstallScope.PROJECT,
            candidates=[
                ResolvedPluginCandidate("first", first_peer),
                ResolvedPluginCandidate("second", second_peer),
            ],
            capability=_copilot_capability(),
        )
    assert not catalog_path_for(peer_modules).exists()

    owner_project, owner_modules = _project(tmp_path / "owner-flip")
    owner = owner_modules / "owner"
    claimant = owner_modules / "claimant"
    write_agent_plugin(owner, name="shared-plugin")
    synchronize_copilot_plugins(
        project_root=owner_project,
        modules_dir=owner_modules,
        scope=InstallScope.PROJECT,
        candidates=[ResolvedPluginCandidate("owner", owner)],
        capability=_copilot_capability(),
    )
    write_agent_plugin(claimant, name="shared-plugin")
    with pytest.raises(CopilotSettingsCollisionError, match=r"silently re-point"):
        synchronize_copilot_plugins(
            project_root=owner_project,
            modules_dir=owner_modules,
            scope=InstallScope.PROJECT,
            candidates=[ResolvedPluginCandidate("claimant", claimant, direct=False)],
            capability=_copilot_capability(),
        )
    assert read_ledger(ledger_path_for(owner_modules)).owner_of("shared-plugin@apm") == "owner"

    promoted = synchronize_copilot_plugins(
        project_root=owner_project,
        modules_dir=owner_modules,
        scope=InstallScope.PROJECT,
        candidates=[ResolvedPluginCandidate("claimant", claimant, direct=True)],
        capability=_copilot_capability(),
    )
    assert promoted.plugin_names == ("shared-plugin",)
    assert read_ledger(ledger_path_for(owner_modules)).owner_of("shared-plugin@apm") == "claimant"
    assert read_json(catalog_path_for(owner_modules))["plugins"][0]["source"] == "./claimant"


@pytest.mark.req("req-tg-013")
def test_native_lifecycle_registration_clauses_persist_in_spec() -> None:
    """Silent-deletion detector for the req-tg-013 normative clauses."""
    assert_spec_contains(
        "MAY register that dependency with a target-native plugin host when",
        "MUST NOT locate, invoke, or version-check\nthe target-native host binary",
        "deployed exactly once",
        "registration MUST reference the materialized\n"
        "package in place beneath the resolved dependency root",
        "MAY re-adopt an existing entry only\nwhen it is exactly",
        "MUST refuse an existing conflicting entry that uses the reserved\nmarketplace identifier",
        "MUST preserve\nunrelated host JSON keys and values semantically",
        "Invalid JSON, including JSONC comments, MUST fail closed\nbefore overwrite",
        "MUST commit as\none rollback unit",
        "directly declared dependency MUST win over a transitive\ndependency",
        "MUST NOT silently repoint a ledger-recorded owner to a\ndifferent admitted transitive claimant",
        "MUST omit every ambiguous\nor changed-owner entry",
        "Removal MUST retire only entries in that consumer-owned rollback unit",
        "A consumer MUST exclude from plugin-name claimant selection every Agent Plugin\n"
        "dependency that did not pass the admission conditions above. It MUST NOT\n"
        "register such a dependency",
    )
