"""Native Agent Plugins v1 lifecycle-registration conformance -- sec.8.5.7.

req-tg-013 is the Mode C amendment that records a qualified,
machine-verifiable consumer lifecycle. These tests drive the real install,
uninstall and admission owners and assert the observable guarantees the
requirement encodes, on the #3921 route that activates Agent Plugins from
``.agents/plugins``:

* one aggregate directory marketplace per scope covering direct + transitive
  Agent Plugin dependencies, with ownership recorded in ``apm.lock.yaml``;
* removal retires only what the previous lock recorded, refuses a foreign
  entry under APM's marketplace name, and preserves unrelated settings values
  semantically even when stable serialization reformats the document;
* target-native registration applies only when the effective targets include
  ``copilot``; other targets remain behind the req-tg-011 boundary.

Clauses the #3921 route no longer implements as written (in-place reference,
direct-over-transitive precedence) are waived with their reason.

The suite is hermetic: capability resolution is a pure target-selection
decision and never discovers or executes a Copilot runtime.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from apm_cli import config
from apm_cli.agent_plugins.errors import AgentPluginDeploymentBoundaryError
from apm_cli.cli import cli
from apm_cli.copilot_plugins.capability import (
    native_registration_scope,
    resolve_native_registration_capability,
)
from apm_cli.core.scope import InstallScope
from apm_cli.deps.lockfile import LockFile
from apm_cli.install.drift import CheckLogger, ReplayConfig, run_replay
from apm_cli.install.dry_run_plan import ProspectiveInstallPlan
from apm_cli.install.native_plugin_admission import finalize_native_plugin
from apm_cli.install.template import preflight_agent_plugin_dry_run
from apm_cli.models.apm_package import APMPackage
from apm_cli.native_plugins.record import lock_native_plugins
from apm_cli.utils.diagnostics import DiagnosticCollector
from tests.integration.test_native_plugin_audit_lifecycle import (
    _run_install,
    _snapshot_tree,
    _write_project_with_skill,
)
from tests.spec_conformance._helpers import assert_spec_contains, waive
from tests.unit.copilot_plugins._builders import (
    copilot_project_settings,
    native_catalog_path,
    native_marketplace,
    native_plugins_root,
    read_json,
    write_agent_plugin,
    write_legacy_package,
)
from tests.utils.artifact_snapshot import ArtifactSnapshot
from tests.utils.local_package import LocalPackageFactory

_COPILOT_TARGET = SimpleNamespace(name="copilot")


@pytest.mark.req("req-tg-011")
def test_transitive_native_preview_preflight_preserves_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bind the existing native dry-run nonmutation clause to transitive preflight."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("APM_HOME", str(home / ".apm"))
    monkeypatch.setattr(config, "CONFIG_DIR", str(home / ".apm"))
    monkeypatch.setattr(config, "CONFIG_FILE", str(home / ".apm" / "config.json"))
    monkeypatch.setattr(config, "_config_cache", None)
    factory = LocalPackageFactory(tmp_path / "sources")
    write_agent_plugin(tmp_path / "sources" / "native", name="native", with_mcp=False)
    factory.create("parent", dependencies=("../native",))
    consumer = factory.create("consumer", dependencies=("../parent",), targets=("copilot",))
    package = APMPackage.from_apm_yml(consumer.manifest_path, create_config=False)
    ctx = SimpleNamespace(
        project_root=consumer.root,
        apm_dir=consumer.root,
        scope=InstallScope.PROJECT,
        target="copilot",
        create_config=False,
        only_packages=None,
        update=False,
        refresh=False,
        frozen=False,
        logger=None,
    )
    monkeypatch.chdir(consumer.root)
    before = ArtifactSnapshot.capture(tmp_path)
    plan = ProspectiveInstallPlan.from_install_context(
        package, ctx, should_install_apm=True, should_install_mcp=False
    )
    assert not plan.dependency_preview_errors
    assert len(plan.selected_apm_dependencies) == 2
    preflight_agent_plugin_dry_run(ctx, list(plan.selected_apm_dependencies), apm_package=package)
    assert ArtifactSnapshot.capture(tmp_path) == before
    assert not (home / ".apm" / "config.json").exists()


@pytest.mark.req("req-tg-011")
def test_cache_only_replay_preserves_native_target_exclusion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Replay excludes native deployment without suppressing ordinary skills."""
    project = tmp_path / "project"
    source = tmp_path / "plugins" / "copilot_only"
    write_agent_plugin(source, name="copilot-only", with_mcp=False)
    _write_project_with_skill(project, [str(source)], targets=["copilot"])
    install_res = _run_install(monkeypatch, project, "--target", "copilot")
    assert install_res.exit_code == 0, install_res.output

    _write_project_with_skill(project, [str(source)], targets=["claude"])
    before = _snapshot_tree(project)
    scratch = tmp_path / "replay"
    result = run_replay(
        ReplayConfig(
            project_root=project,
            lockfile_path=project / "apm.lock.yaml",
            targets=frozenset({"claude"}),
            scratch_root=scratch,
        ),
        CheckLogger(),
    )

    assert result == scratch
    assert "Replayed 1 package(s)" in capsys.readouterr().err
    assert (scratch / ".claude/skills/local-skill/SKILL.md").is_file()
    assert not native_catalog_path(scratch).exists()
    assert not native_plugins_root(scratch).exists()
    assert not (scratch / ".claude/skills/copilot-only-skill").exists()
    assert before == _snapshot_tree(project)


_MARKETPLACES = "extraKnownMarketplaces"
_ENABLED = "enabledPlugins"
_JSONC = '{\n  // keep this comment\n  "enabledPlugins": {}\n}\n'


def _write_project(project: Path, dependencies: list[str]) -> None:
    project.mkdir(parents=True, exist_ok=True)
    (project / "apm.yml").write_text(
        json.dumps(
            {
                "name": "consumer",
                "version": "1.0.0",
                "description": "consumer",
                "dependencies": {"apm": dependencies},
            }
        ),
        encoding="ascii",
    )


def _cli(monkeypatch: pytest.MonkeyPatch, project: Path, *args: str):
    monkeypatch.chdir(project)
    return CliRunner().invoke(cli, list(args), catch_exceptions=False)


def _install(monkeypatch: pytest.MonkeyPatch, project: Path):
    return _cli(monkeypatch, project, "install", "--no-policy", "--target", "copilot")


def _flat(text: str) -> str:
    return " ".join(text.split())


@pytest.mark.req("req-tg-013")
def test_native_registration_is_aggregate_and_consumer_owned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One aggregate registration, proven by consumer-owned lock state."""
    project = tmp_path / "project"
    direct = write_agent_plugin(tmp_path / "source" / "direct", name="direct-plugin")
    write_agent_plugin(tmp_path / "source" / "transitive", name="transitive-plugin")
    parent = write_legacy_package(
        tmp_path / "source" / "parent", name="parent", dependencies=["../transitive"]
    )
    _write_project(project, [str(direct), str(parent)])

    result = _install(monkeypatch, project)

    assert result.exit_code == 0, result.output
    # One aggregate catalog per scope covers direct AND transitive deps.
    marketplace = native_marketplace(project)
    assert read_json(native_catalog_path(project))["plugins"] == [
        {"name": "direct-plugin", "source": "./direct-plugin"},
        {"name": "transitive-plugin", "source": "./transitive-plugin"},
    ]
    for name in ("direct-plugin", "transitive-plugin"):
        assert (native_plugins_root(project) / name / "plugin.json").is_file()

    settings = read_json(copilot_project_settings(project))
    assert settings[_MARKETPLACES] == {
        marketplace: {"source": {"source": "directory", "path": "./.agents/plugins"}}
    }
    assert settings[_ENABLED] == {
        f"direct-plugin@{marketplace}": True,
        f"transitive-plugin@{marketplace}": True,
    }

    # Ownership lives in consumer-owned state: the lock, not a guessed value shape.
    records = lock_native_plugins(LockFile.read(project / "apm.lock.yaml")).values()
    assert sorted((r.name, r.binding) for r in records) == [
        ("direct-plugin", (marketplace, "./.agents/plugins")),
        ("transitive-plugin", (marketplace, "./.agents/plugins")),
    ]
    assert not (project / "apm_modules" / ".github" / "plugin").exists()


@pytest.mark.req("req-tg-013")
def test_native_registration_references_the_materialized_package_in_place() -> None:
    """In-place reference beneath the dependency root no longer holds."""
    waive(
        "#3921 activates an Agent Plugin from an intact copy deployed to "
        ".agents/plugins/<name>, verified against the lock fingerprint, instead of "
        "referencing apm_modules in place; the spec clause awaits its amendment."
    )


@pytest.mark.req("req-tg-013")
def test_removal_is_exact_and_unowned_entries_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removal retires only owned rows; a foreign entry is never overwritten."""
    project = tmp_path / "project"
    source = write_agent_plugin(tmp_path / "source" / "pkg", name="portable-plugin")
    _write_project(project, [str(source)])
    settings_path = copilot_project_settings(project)
    settings_path.parent.mkdir(parents=True)
    original = (
        '{"banner":"keep me","extraKnownMarketplaces":{"team":{"source":'
        '{"source":"directory","path":"x"}}},"enabledPlugins":'
        '{"team-plugin@team":true}}'
    )
    settings_path.write_text(original, encoding="ascii")

    assert _install(monkeypatch, project).exit_code == 0
    marketplace = native_marketplace(project)
    after_register = read_json(settings_path)
    assert after_register[_ENABLED][f"portable-plugin@{marketplace}"] is True
    assert after_register["banner"] == "keep me"
    assert settings_path.read_text(encoding="ascii") != original

    # Uninstall retires exactly the entries the previous lock recorded.
    removed = _cli(monkeypatch, project, "uninstall", str(source))
    assert removed.exit_code == 0, removed.output
    after_remove = read_json(settings_path)
    assert after_remove == {
        "banner": "keep me",
        _MARKETPLACES: {"team": {"source": {"source": "directory", "path": "x"}}},
        _ENABLED: {"team-plugin@team": True},
    }
    assert not native_plugins_root(project).exists()

    # A same-name marketplace APM did not write is refused, not overwritten.
    _write_project(project, [str(source)])
    foreign = json.dumps(
        {_MARKETPLACES: {marketplace: {"source": {"source": "git", "path": "x"}}}}, indent=2
    )
    settings_path.write_text(foreign, encoding="ascii")
    refused = _install(monkeypatch, project)
    assert refused.exit_code != 0
    assert "Native plugin declaration failed" in _flat(refused.output)
    assert settings_path.read_text(encoding="ascii") == foreign
    assert not native_plugins_root(project).exists()


@pytest.mark.req("req-tg-013")
def test_non_copilot_target_creates_no_native_registration(tmp_path: Path) -> None:
    """A target set without Copilot creates no target registration state."""
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

    # Finalization under a non-Copilot target queues nothing for activation.
    root = write_agent_plugin(tmp_path / "pkg", name="portable-plugin")
    plugin = SimpleNamespace(
        identity=SimpleNamespace(name="portable-plugin"),
        components=SimpleNamespace(mcp_servers=()),
    )
    ctx = SimpleNamespace(
        exec_trust_ctx=None,
        package_exec_status={},
        scope=InstallScope.PROJECT,
        claude_native_plugins={},
    )
    finalized = finalize_native_plugin(
        {"native_plugin": False},
        SimpleNamespace(package=SimpleNamespace(agent_plugin=plugin), install_path=root),
        "pkg",
        [SimpleNamespace(name="claude")],
        hooks_approved=True,
        mcp_approved=True,
        bin_approved=True,
        canvas_approved=True,
        lsp_approved=True,
        ctx=ctx,
        diagnostics=DiagnosticCollector(),
        logger=None,
    )
    assert finalized["native_plugin"] is False
    assert ctx.claude_native_plugins == {}


@pytest.mark.req("req-tg-013")
def test_missing_previous_lock_never_adopts_or_retires_existing_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exact APM state is restored; entries no lock records are reported, not removed."""
    project = tmp_path / "project"
    source = write_agent_plugin(tmp_path / "source" / "pkg", name="portable-plugin")
    _write_project(project, [str(source)])
    assert _install(monkeypatch, project).exit_code == 0
    marketplace = native_marketplace(project)
    (project / "apm.lock.yaml").unlink()
    settings_path = copilot_project_settings(project)
    settings = read_json(settings_path)
    settings[_ENABLED][f"manual@{marketplace}"] = True
    settings[_ENABLED]["team@other"] = True
    settings_path.write_text(json.dumps(settings), encoding="ascii")

    result = _install(monkeypatch, project)

    assert result.exit_code == 0, result.output
    assert "but no previous apm.lock.yaml records them" in _flat(result.output)
    assert read_json(settings_path)[_ENABLED] == {
        f"portable-plugin@{marketplace}": True,
        f"manual@{marketplace}": True,
        "team@other": True,
    }
    records = lock_native_plugins(LockFile.read(project / "apm.lock.yaml")).values()
    assert [r.name for r in records] == ["portable-plugin"]


@pytest.mark.req("req-tg-013")
def test_invalid_settings_fail_closed_without_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invalid JSON/JSONC is never overwritten by registration."""
    project = tmp_path / "project"
    source = write_agent_plugin(tmp_path / "source" / "pkg", name="portable-plugin")
    _write_project(project, [str(source)])
    settings_path = copilot_project_settings(project)
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(_JSONC, encoding="ascii")

    result = _install(monkeypatch, project)

    assert result.exit_code != 0
    assert "as JSON; APM did not change it" in _flat(result.output)
    assert settings_path.read_text(encoding="ascii") == _JSONC
    assert not native_plugins_root(project).exists()


@pytest.mark.req("req-tg-013")
def test_same_plugin_name_claimants_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two admitted dependencies claiming one plugin name refuse; nothing is written."""
    project = tmp_path / "project"
    first = write_agent_plugin(tmp_path / "source" / "first", name="shared-plugin")
    second = write_agent_plugin(tmp_path / "source" / "second", name="shared-plugin")
    _write_project(project, [str(first), str(second)])

    result = _install(monkeypatch, project)

    assert result.exit_code != 0
    assert "Native plugin name collision:" in _flat(result.output)
    assert "declares 'shared-plugin'" in _flat(result.output)
    assert not native_plugins_root(project).exists()
    assert not copilot_project_settings(project).exists()


@pytest.mark.req("req-tg-013")
def test_plugin_name_precedence_and_owner_identity_are_deterministic() -> None:
    """Direct-over-transitive precedence and ledger owner pinning are gone."""
    waive(
        "#3921 refuses every plugin-name collision instead of letting a direct "
        "dependency win over a transitive one, and records ownership per lock entry "
        "rather than in a ledger, so there is no owner to re-point; see "
        "test_same_plugin_name_claimants_are_refused. The precedence clauses await "
        "their spec amendment."
    )


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
