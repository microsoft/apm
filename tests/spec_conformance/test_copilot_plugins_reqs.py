"""Native Agent Plugins v1 lifecycle-registration conformance -- sec.8.5.7.

req-tg-013 is the Mode C amendment that records a qualified,
machine-verifiable consumer lifecycle. These tests drive the real
registration owner in ``src/apm_cli/copilot_plugins/`` and assert the
observable guarantees the requirement encodes:

* one aggregate registration per scope covering direct + transitive
  Agent Plugin dependencies;
* the registration references the materialized package in place and
  never copies it into a private plugin store;
* ownership is proven from consumer-owned state, so a removal retires
  exactly the entries the consumer created, unrelated host settings
  stay byte-identical, and an entry the consumer does not own is
  refused rather than overwritten;
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
    settings_path.write_text(
        json.dumps(
            {
                "banner": "keep me",
                EXTRA_MARKETPLACES_KEY: {"team": {"source": {"source": "directory", "path": "x"}}},
                ENABLED_PLUGINS_KEY: {"team-plugin@team": True},
            },
            indent=2,
        ),
        encoding="ascii",
    )

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
    # Every unrelated key survives byte-for-byte.
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
def test_non_copilot_target_falls_back_to_the_boundary(tmp_path: Path) -> None:
    """A target set without Copilot remains behind the req-tg-011 boundary."""
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
def test_native_lifecycle_registration_clauses_persist_in_spec() -> None:
    """Silent-deletion detector for the req-tg-013 normative clauses."""
    assert_spec_contains(
        "MAY register that dependency with a target-native",
        "deployed exactly once",
        "reference the materialized package in place beneath the resolved dependency",
        "MUST refuse the operation rather than overwrite",
        "install scope MUST cover both the directly declared and the transitively",
        "fail-closed deployment boundary",
    )
