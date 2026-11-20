"""Native Agent Plugins v1 lifecycle-registration conformance -- sec.8.5.7.

req-tg-013 is the Mode C amendment that records a qualified,
machine-verifiable consumer lifecycle. Since #3921 an admitted Agent Plugin
takes the shared native-plugin route; these tests drive the real install,
uninstall and audit commands and assert the observable guarantees:

* one aggregate catalog per scope covering direct + transitive Agent Plugin
  dependencies, each deployed as an exact copy of the materialized package
  under ``.agents/plugins`` and declared in the committed
  ``.github/copilot/settings.json``;
* ownership lives in the consumer's ``apm.lock.yaml`` ``native_plugin``
  records: removal retires exactly those entries, a same-name marketplace
  with another source is refused, and unrelated settings values survive;
* invalid settings fail closed before any overwrite;
* among claimants of one plugin name a direct dependency wins, same-precedence
  claimants fail, and a lock-recorded owner is never repointed to a
  transitive claimant;
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
from click.testing import CliRunner

from apm_cli import config
from apm_cli.agent_plugins.errors import AgentPluginDeploymentBoundaryError
from apm_cli.cli import cli
from apm_cli.copilot_plugins.capability import (
    native_registration_scope,
    resolve_native_registration_capability,
)
from apm_cli.copilot_plugins.constants import (
    ENABLED_PLUGINS_KEY,
    EXTRA_MARKETPLACES_KEY,
)
from apm_cli.copilot_plugins.registrar import catalog_path_for, ledger_path_for
from apm_cli.core.scope import InstallScope
from apm_cli.deps.lockfile import LockFile
from apm_cli.install.drift import CheckLogger, ReplayConfig, run_replay
from apm_cli.install.dry_run_plan import ProspectiveInstallPlan
from apm_cli.install.template import preflight_agent_plugin_dry_run
from apm_cli.models.apm_package import APMPackage, clear_apm_yml_cache
from apm_cli.native_plugins.binding import project_marketplace_name
from apm_cli.utils.content_hash import native_plugin_files
from tests.integration.test_native_plugin_audit_lifecycle import (
    _run_install,
    _snapshot_tree,
    _write_project_with_skill,
)
from tests.spec_conformance._helpers import assert_spec_contains
from tests.unit.copilot_plugins._builders import (
    read_json,
    write_agent_plugin,
    write_legacy_package,
)
from tests.utils.artifact_snapshot import ArtifactSnapshot
from tests.utils.local_package import LocalPackageFactory


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
    assert not (scratch / ".agents/plugins").exists()
    assert not catalog_path_for(scratch / "apm_modules").exists()
    assert not (scratch / ".claude/skills/copilot-only-skill").exists()
    assert before == _snapshot_tree(project)


_MARKETPLACE = project_marketplace_name("consumer")


def _write_consumer(project: Path, dependencies: list[str]) -> None:
    project.mkdir(parents=True, exist_ok=True)
    (project / "apm.yml").write_text(
        json.dumps({"name": "consumer", "version": "1.0.0", "dependencies": {"apm": dependencies}}),
        encoding="ascii",
    )
    # In-process CLI runs share the apm.yml parse cache; drop the stale entry.
    clear_apm_yml_cache()


def _cli(monkeypatch: pytest.MonkeyPatch, project: Path, *args: str):
    monkeypatch.chdir(project)
    return CliRunner().invoke(cli, list(args), catch_exceptions=False)


def _install(monkeypatch: pytest.MonkeyPatch, project: Path, target: str = "copilot"):
    return _cli(monkeypatch, project, "install", "--no-policy", "--target", target)


def _settings_path(project: Path) -> Path:
    return project / ".github" / "copilot" / "settings.json"


def _plugins_root(project: Path) -> Path:
    return project / ".agents" / "plugins"


def _catalog(project: Path) -> dict:
    return read_json(_plugins_root(project) / ".claude-plugin" / "marketplace.json")


def _file_bytes(root: Path) -> dict[str, bytes]:
    return {path.as_posix(): (root / path).read_bytes() for path in native_plugin_files(root)}


def _assert_nothing_declared(project: Path) -> None:
    assert not _plugins_root(project).exists()
    assert not _settings_path(project).exists()
    assert not (project / ".github" / "copilot" / "settings.local.json").exists()
    assert not (project / ".claude" / "settings.json").exists()
    assert not catalog_path_for(project / "apm_modules").exists()
    assert not ledger_path_for(project / "apm_modules").exists()


@pytest.mark.req("req-tg-013")
def test_native_registration_is_aggregate_in_place_and_consumer_owned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One aggregate declaration of exact deployed copies, owned by the lock."""
    copilot_home = tmp_path / "copilot-home"
    monkeypatch.setenv("COPILOT_HOME", str(copilot_home))
    project = tmp_path / "project"
    sources = tmp_path / "sources"
    write_agent_plugin(sources / "direct", name="direct-plugin")
    write_agent_plugin(sources / "nested", name="transitive-plugin")
    write_legacy_package(sources / "parent", name="parent", dependencies=["../nested"])
    _write_consumer(project, [str(sources / "direct"), str(sources / "parent")])

    result = _install(monkeypatch, project)

    assert result.exit_code == 0, result.output
    # One aggregate catalog per scope covers direct AND transitive deps.
    assert _catalog(project) == {
        "name": _MARKETPLACE,
        "owner": {"name": "APM"},
        "plugins": [
            {"name": "direct-plugin", "source": "./direct-plugin"},
            {"name": "transitive-plugin", "source": "./transitive-plugin"},
        ],
    }
    # Each deployed root is an exact copy of its materialized package.
    modules = project / "apm_modules"
    for name, materialized in (
        ("direct-plugin", modules / "_local" / "direct"),
        ("transitive-plugin", next(modules.rglob("nested"))),
    ):
        assert _file_bytes(_plugins_root(project) / name) == _file_bytes(materialized)

    # The committed activation surface points at the project-relative root.
    assert read_json(_settings_path(project)) == {
        EXTRA_MARKETPLACES_KEY: {
            _MARKETPLACE: {"source": {"source": "directory", "path": "./.agents/plugins"}}
        },
        ENABLED_PLUGINS_KEY: {
            f"direct-plugin@{_MARKETPLACE}": True,
            f"transitive-plugin@{_MARKETPLACE}": True,
        },
    }
    # Nothing lands in a private Copilot store.
    assert not copilot_home.exists()

    # Ownership lives in consumer-owned state: the lock's native records.
    lock = LockFile.read(project / "apm.lock.yaml")
    records = sorted(
        (dep.native_plugin.name, dep.native_plugin.marketplace, dep.native_plugin.root)
        for dep in lock.dependencies.values()
        if dep.native_plugin is not None
    )
    assert records == [
        ("direct-plugin", _MARKETPLACE, ".agents/plugins/direct-plugin"),
        ("transitive-plugin", _MARKETPLACE, ".agents/plugins/transitive-plugin"),
    ]


@pytest.mark.req("req-tg-013")
def test_removal_is_exact_and_unowned_entries_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removal retires only owned rows; a foreign entry is never overwritten."""
    project = tmp_path / "project"
    source = write_agent_plugin(tmp_path / "source" / "portable", name="portable-plugin")
    _write_consumer(project, [str(source)])
    settings_path = _settings_path(project)
    settings_path.parent.mkdir(parents=True)
    user_settings = {
        "banner": "keep me",
        EXTRA_MARKETPLACES_KEY: {"team": {"source": {"source": "directory", "path": "x"}}},
        ENABLED_PLUGINS_KEY: {"team-plugin@team": True, f"opted-out@{_MARKETPLACE}": False},
    }
    original = json.dumps(user_settings, separators=(",", ":"))
    settings_path.write_text(original, encoding="ascii")

    assert _install(monkeypatch, project).exit_code == 0
    after_register = read_json(settings_path)
    assert after_register[ENABLED_PLUGINS_KEY][f"portable-plugin@{_MARKETPLACE}"] is True
    assert after_register["banner"] == "keep me"
    assert settings_path.read_text(encoding="ascii") != original

    # Uninstall retires exactly APM's rows; everything else survives semantically.
    result = _cli(monkeypatch, project, "uninstall", str(source))
    assert result.exit_code == 0, result.output
    assert read_json(settings_path) == user_settings
    assert not _plugins_root(project).exists()

    # A same-name marketplace APM does not own is refused, not overwritten.
    foreign = json.dumps(
        {EXTRA_MARKETPLACES_KEY: {_MARKETPLACE: {"source": {"source": "git", "path": "x"}}}},
        indent=2,
    )
    settings_path.write_text(foreign, encoding="ascii")
    result = _install(monkeypatch, project)
    assert result.exit_code != 0, result.output
    assert "with another source" in " ".join(result.output.split())
    assert settings_path.read_text(encoding="ascii") == foreign
    assert not _plugins_root(project).exists()


@pytest.mark.req("req-tg-013")
def test_non_copilot_target_creates_no_native_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    # An install whose targets exclude Copilot declares the plugin nowhere.
    project = tmp_path / "project"
    sources = tmp_path / "sources"
    write_agent_plugin(sources / "nested", name="portable-plugin")
    write_legacy_package(sources / "parent", name="parent", dependencies=["../nested"])
    _write_consumer(project, [str(sources / "parent")])

    result = _install(monkeypatch, project, target="claude")

    assert result.exit_code == 0, result.output
    assert (project / ".claude" / "skills" / "parent-skill" / "SKILL.md").is_file()
    _assert_nothing_declared(project)
    lock = LockFile.read(project / "apm.lock.yaml")
    assert all(dep.native_plugin is None for dep in lock.dependencies.values())


@pytest.mark.req("req-tg-013")
def test_missing_lock_re_adopts_exact_state_and_removes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the lock APM keeps exact entries and never removes unproven ones."""
    project = tmp_path / "project"
    source = write_agent_plugin(tmp_path / "source" / "portable", name="portable-plugin")
    _write_consumer(project, [str(source)])
    assert _install(monkeypatch, project).exit_code == 0
    (project / "apm.lock.yaml").unlink()
    settings_path = _settings_path(project)
    settings = read_json(settings_path)
    settings[ENABLED_PLUGINS_KEY][f"manual@{_MARKETPLACE}"] = True
    settings[ENABLED_PLUGINS_KEY]["team@other"] = True
    settings_path.write_text(json.dumps(settings), encoding="ascii")

    result = _install(monkeypatch, project)

    assert result.exit_code == 0, result.output
    assert "no previous apm.lock.yaml records them" in " ".join(result.output.split())
    assert read_json(settings_path)[ENABLED_PLUGINS_KEY] == {
        f"portable-plugin@{_MARKETPLACE}": True,
        f"manual@{_MARKETPLACE}": True,
        "team@other": True,
    }
    lock = LockFile.read(project / "apm.lock.yaml")
    assert [dep.native_plugin.name for dep in lock.dependencies.values()] == ["portable-plugin"]


@pytest.mark.req("req-tg-013")
def test_invalid_settings_fail_closed_without_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invalid JSON/JSONC is never overwritten by registration."""
    project = tmp_path / "project"
    source = write_agent_plugin(tmp_path / "source" / "portable", name="portable-plugin")
    _write_consumer(project, [str(source)])
    settings_path = _settings_path(project)
    settings_path.parent.mkdir(parents=True)
    original = '{\n  // keep this comment\n  "enabledPlugins": {}\n}\n'
    settings_path.write_text(original, encoding="ascii")

    result = _install(monkeypatch, project)

    assert result.exit_code != 0, result.output
    assert "APM did not change it" in " ".join(result.output.split())
    assert settings_path.read_text(encoding="ascii") == original
    assert not _plugins_root(project).exists()
    assert not (project / "apm.lock.yaml").exists()


def _deployed_skill(project: Path, name: str) -> str:
    skill = _plugins_root(project) / name / "skills" / f"{name}-skill" / "SKILL.md"
    return skill.read_text(encoding="ascii")


def _native_owners(project: Path) -> list[tuple[str, int]]:
    lock = LockFile.read(project / "apm.lock.yaml")
    return [
        (dep.native_plugin.name, dep.depth)
        for dep in lock.dependencies.values()
        if dep.native_plugin is not None
    ]


@pytest.mark.req("req-tg-013")
def test_plugin_name_precedence_and_owner_identity_are_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct wins; ambiguous peers and transitive ownership flips refuse."""
    sources = tmp_path / "sources"
    write_agent_plugin(sources / "direct", name="shared-plugin", skill_body="FROM_DIRECT")
    write_agent_plugin(sources / "peer", name="shared-plugin", skill_body="FROM_PEER")
    write_agent_plugin(sources / "nested", name="shared-plugin", skill_body="FROM_NESTED")
    write_legacy_package(sources / "parent", name="parent", dependencies=["../nested"])

    # A directly declared dependency wins over a transitive claimant.
    direct_project = tmp_path / "direct-wins"
    _write_consumer(direct_project, [str(sources / "direct"), str(sources / "parent")])
    result = _install(monkeypatch, direct_project)
    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert "Native plugin 'shared-plugin' is declared directly by" in flat
    # Rich may wrap inside a long path, so compare with all whitespace removed.
    assert f"activating{sources / 'direct'}andskipping" in "".join(result.output.split())
    assert "FROM_DIRECT" in _deployed_skill(direct_project, "shared-plugin")
    assert _catalog(direct_project)["plugins"] == [
        {"name": "shared-plugin", "source": "./shared-plugin"}
    ]
    assert _native_owners(direct_project) == [("shared-plugin", 1)]

    # Two claimants at the same precedence fail closed and declare nothing.
    peer_project = tmp_path / "peer-collision"
    _write_consumer(peer_project, [str(sources / "direct"), str(sources / "peer")])
    result = _install(monkeypatch, peer_project)
    assert result.exit_code != 0, result.output
    assert "Native plugin name 'shared-plugin' is claimed by" in " ".join(result.output.split())
    assert "at the same precedence" in " ".join(result.output.split())
    _assert_nothing_declared(peer_project)
    assert not (peer_project / "apm.lock.yaml").exists()

    # A lock-recorded owner is never silently repointed to a transitive claimant.
    owner_project = tmp_path / "owner-flip"
    _write_consumer(owner_project, [str(sources / "direct")])
    assert _install(monkeypatch, owner_project).exit_code == 0
    _write_consumer(owner_project, [str(sources / "parent")])
    result = _install(monkeypatch, owner_project)
    assert result.exit_code == 0, result.output
    assert "it is not repointed to the transitive dependency" in " ".join(result.output.split())
    assert _native_owners(owner_project) == []
    assert not _plugins_root(owner_project).exists()
    # Sections APM emptied stay as empty objects (req-na-004).
    assert read_json(_settings_path(owner_project)) == {
        EXTRA_MARKETPLACES_KEY: {},
        ENABLED_PLUGINS_KEY: {},
    }

    # Declaring the claimant directly is the explicit way to take the name.
    _write_consumer(owner_project, [str(sources / "parent"), str(sources / "nested")])
    result = _install(monkeypatch, owner_project)
    assert result.exit_code == 0, result.output
    assert "FROM_NESTED" in _deployed_skill(owner_project, "shared-plugin")
    assert _native_owners(owner_project) == [("shared-plugin", 1)]
    assert read_json(_settings_path(owner_project))[ENABLED_PLUGINS_KEY] == {
        f"shared-plugin@{_MARKETPLACE}": True
    }


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
