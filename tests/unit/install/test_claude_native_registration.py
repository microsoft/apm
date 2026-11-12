"""Native metadata lifecycle crosses only the external Claude command seam."""

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from apm_cli.claude_plugins.admission import (
    ClaudeNativePlugin,
    ClaudeNativePluginError,
    fingerprint_native_claude_source,
)
from apm_cli.claude_plugins.client import ClaudePluginCLI
from apm_cli.claude_plugins.registrar import (
    CATALOG_RELATIVE,
    LEDGER_RELATIVE,
    MARKETPLACE_NAME,
    resync_claude_plugins,
    synchronize_claude_plugins,
)
from apm_cli.core.scope import InstallScope

pytestmark = pytest.mark.component


def test_native_child_environment_restores_libraries_and_omits_platform_tokens(
    monkeypatch,
) -> None:
    """Reuse canonical subprocess and token owners without changing parent/native auth."""
    monkeypatch.setattr("shutil.which", lambda name: "/native/claude")
    monkeypatch.setattr("sys.frozen", True, raising=False)
    supplied = {
        "GH_TOKEN": "sentinel-gh",
        "GITHUB_APM_PAT_ORG": "sentinel-scoped",
        "APM_REGISTRY_TOKEN_PRIVATE": "sentinel-registry",
        "LD_LIBRARY_PATH": "/bundle",
        "LD_LIBRARY_PATH_ORIG": "/user",
        "CLAUDE_CONFIG_DIR": "/owned-claude",
        "ANTHROPIC_API_KEY": "sentinel-native",
    }
    for key, value in supplied.items():
        monkeypatch.setenv(key, value)
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="2.1.291 (Claude Code)"))
    monkeypatch.setattr(subprocess, "run", run)
    assert ClaudePluginCLI().run("--version") == "2.1.291 (Claude Code)"
    env = run.call_args.kwargs["env"]
    assert not {"GH_TOKEN", "GITHUB_APM_PAT_ORG", "APM_REGISTRY_TOKEN_PRIVATE"} & env.keys()
    assert env["LD_LIBRARY_PATH"] == "/user"
    assert "LD_LIBRARY_PATH_ORIG" not in env
    assert env["CLAUDE_CONFIG_DIR"] == "/owned-claude"
    assert env["ANTHROPIC_API_KEY"] == "sentinel-native"
    assert all(os.environ[key] == value for key, value in supplied.items())


def test_native_transport_failure_keeps_targets_without_echoing_native_stderr(monkeypatch) -> None:
    """Do not leak captured output or prescribe a target-dropping install retry."""
    monkeypatch.setattr("shutil.which", lambda name: "/native/claude")
    monkeypatch.setattr(
        subprocess,
        "run",
        Mock(
            return_value=SimpleNamespace(returncode=1, stdout="private-out", stderr="private-err")
        ),
    )
    with pytest.raises(ClaudeNativePluginError, match="original target selection") as error:
        ClaudePluginCLI().run("--version")
    assert "private" not in str(error.value)


@pytest.fixture
def native(monkeypatch, tmp_path):
    state = {
        "plugins": {},
        "marketplaces": [],
        "calls": [],
        "fail": False,
        "modules_root": tmp_path,
    }
    monkeypatch.setattr(ClaudePluginCLI, "__init__", lambda self: None)

    def run(self, *args):
        state["calls"].append(args)
        if args == ("--version",):
            return "2.1.291 (Claude Code)"
        if args == ("plugin", "marketplace", "list", "--json"):
            return json.dumps(state["marketplaces"])
        if args == ("plugin", "list", "--json"):
            return json.dumps(list(state["plugins"].values()))
        if args[:3] == ("plugin", "marketplace", "add"):
            state["marketplaces"] = [
                {
                    "name": MARKETPLACE_NAME,
                    "source": "directory",
                    "path": args[3],
                    "installLocation": args[3],
                }
            ]
            return ""
        plugin_id = args[2]
        if args[1] == "uninstall":
            assert args[-1] == "--keep-data"
            if state.get("fail_uninstall"):
                raise ClaudeNativePluginError("interrupted before native removal")
            state["plugins"].pop(plugin_id)
            return ""
        assert args[1] in {"install", "update"}
        catalog = json.loads((state["modules_root"] / CATALOG_RELATIVE).read_text())
        entry = next(item for item in catalog["plugins"] if item["name"] == plugin_id.split("@")[0])
        assert entry["defaultEnabled"] is False
        previous = state["plugins"].get(plugin_id)
        state["plugins"][plugin_id] = {
            "id": plugin_id,
            "scope": "user",
            "enabled": previous["enabled"] if previous else False,
            "readFromFolder": str((state["modules_root"] / entry["source"]).resolve()),
            "installPath": "/not/the/effective/root",
        }
        if state["fail"]:
            raise ClaudeNativePluginError("interrupted after native install")
        return ""

    monkeypatch.setattr(ClaudePluginCLI, "run", run)
    return state


def _sync(root, names, **kwargs):
    plans = []
    for name in names:
        source = root / name
        source.mkdir(exist_ok=True)
        plans.append(
            ClaudeNativePlugin(
                "owner/" + name, name, source, fingerprint_native_claude_source(source), "fixture"
            )
        )
    synchronize_claude_plugins(
        modules_dir=root,
        scope=InstallScope.USER,
        plugins=plans,
        **kwargs,
    )


def test_install_update_remove_preserves_native_activation(tmp_path, native):
    _sync(tmp_path, ["alpha", "beta"])
    assert all(not row["enabled"] for row in native["plugins"].values())
    alpha_id = "alpha@" + MARKETPLACE_NAME
    beta_id = "beta@" + MARKETPLACE_NAME
    native["plugins"][alpha_id]["enabled"] = True
    _sync(tmp_path, ["alpha", "beta"])
    assert native["plugins"][alpha_id]["enabled"] is True
    assert native["plugins"][beta_id]["enabled"] is False
    _sync(tmp_path, ["beta"])
    assert set(native["plugins"]) == {beta_id}
    _sync(tmp_path, [])
    assert native["plugins"] == {}
    assert json.loads((tmp_path / LEDGER_RELATIVE).read_text())["plugins"] == {}
    assert all("enable" not in call and "disable" not in call for call in native["calls"])


def test_interrupted_native_install_recovers_from_intent(tmp_path, native):
    native["fail"] = True
    with pytest.raises(ClaudeNativePluginError, match="interrupted"):
        _sync(tmp_path, ["alpha"])
    assert (
        "alpha@" + MARKETPLACE_NAME
        in json.loads((tmp_path / LEDGER_RELATIVE).read_text())["plugins"]
    )
    native["fail"] = False
    _sync(tmp_path, ["alpha"])
    assert native["plugins"]["alpha@" + MARKETPLACE_NAME]["enabled"] is False


def test_foreign_marketplace_cannot_be_adopted(tmp_path, native):
    native["marketplaces"] = [
        {
            "name": MARKETPLACE_NAME,
            "source": "directory",
            "path": str(tmp_path),
        }
    ]
    with pytest.raises(ClaudeNativePluginError, match="another source"):
        _sync(tmp_path, ["alpha"])
    assert not (tmp_path / LEDGER_RELATIVE).exists()
    assert all(call[1:3] != ("marketplace", "add") for call in native["calls"])


def test_changed_native_root_refused_before_any_write(tmp_path, native):
    _sync(tmp_path, ["alpha"])
    ledger = (tmp_path / LEDGER_RELATIVE).read_bytes()
    native["plugins"]["alpha@" + MARKETPLACE_NAME]["readFromFolder"] = "/foreign"
    native["calls"].clear()
    with pytest.raises(ClaudeNativePluginError, match="ownership changed"):
        _sync(tmp_path, ["alpha"])
    assert (tmp_path / LEDGER_RELATIVE).read_bytes() == ledger
    assert len(native["calls"]) == 3


def test_name_collision_refused(tmp_path, native):
    _sync(tmp_path, ["alpha"])
    with pytest.raises(ClaudeNativePluginError, match="name collision"):
        synchronize_claude_plugins(
            modules_dir=tmp_path,
            scope=InstallScope.USER,
            plugins=[
                ClaudeNativePlugin(
                    "another/identity",
                    "alpha",
                    tmp_path / "alpha",
                    fingerprint_native_claude_source(tmp_path / "alpha"),
                    "fixture",
                )
            ],
        )


def test_dry_run_has_no_cli_or_file_effects(tmp_path, native):
    _sync(tmp_path, ["alpha"], dry_run=True)
    assert native["calls"] == []
    assert not (tmp_path / LEDGER_RELATIVE).exists()


def test_unsupported_client_has_no_writes(tmp_path, native, monkeypatch):
    monkeypatch.setattr(ClaudePluginCLI, "run", lambda *args: "2.1.126 (Claude Code)")
    with pytest.raises(ClaudeNativePluginError, match="qualified only"):
        _sync(tmp_path, ["alpha"])
    assert not (tmp_path / LEDGER_RELATIVE).exists()


@pytest.mark.parametrize(
    "args",
    [
        ("-p", "hello"),
        ("plugin", "enable", "alpha"),
        ("auth", "login"),
    ],
)
def test_client_cannot_activate_or_run_plugins(args, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/native/claude")
    with pytest.raises(ClaudeNativePluginError, match="non-metadata"):
        ClaudePluginCLI().run(*args)


def test_missing_lock_cannot_remove_owned_native_plugins(tmp_path, native):
    _sync(tmp_path, ["alpha"])
    native["calls"].clear()
    with pytest.raises(ClaudeNativePluginError, match="canonical lockfile"):
        resync_claude_plugins(modules_dir=tmp_path, scope=InstallScope.USER, lockfile=None)
    assert native["calls"] == []
    assert set(native["plugins"]) == {"alpha@" + MARKETPLACE_NAME}


def test_prepared_plugin_missing_from_lock_cannot_report_success(tmp_path, native):
    from apm_cli.deps.lockfile import LockFile

    plan = ClaudeNativePlugin(
        "owner/alpha",
        "alpha",
        tmp_path / "alpha",
        fingerprint_native_claude_source(tmp_path / "alpha"),
    )
    with pytest.raises(ClaudeNativePluginError, match="authorized locked set"):
        resync_claude_plugins(
            modules_dir=tmp_path,
            scope=InstallScope.USER,
            lockfile=LockFile(),
            prepared={plan.dependency_key: plan},
        )
    assert native["calls"] == []


def _locked_native_fixture(project: Path, native, *, local: bool = False):
    """Prepare a real normalized remote plugin without executing its hooks."""
    from apm_cli.claude_plugins.admission import plan_native_claude_plugin
    from apm_cli.deps.lockfile import LockedDependency, LockFile
    from apm_cli.install.cache_pin import write_marker
    from apm_cli.integration.targets import KNOWN_TARGETS
    from apm_cli.utils.content_hash import compute_package_hash
    from tests.unit.install.test_claude_native_hooks import _fixture

    modules = project / "apm_modules"
    native["modules_root"] = modules
    locked = LockedDependency(
        repo_url="../publisher" if local else "owner/hooks",
        source="local" if local else "github",
        local_path="../publisher" if local else None,
        resolved_commit=None if local else "a" * 40,
        package_type="marketplace_plugin",
        target_subset=["claude"],
    )
    info = _fixture(locked.to_dependency_ref().get_install_path(modules))
    (project / "apm.yml").write_text("name: native-audit\nversion: 1.0.0\ntarget: claude\n")
    asset = info.install_path / "__pycache__/publisher-resource.bin"
    asset.parent.mkdir()
    asset.write_bytes(b"owned resource before admission\n")
    (info.install_path / "hooks/probe.cjs").chmod(0o644)
    locked.content_hash = compute_package_hash(info.install_path)
    if local:
        shutil.copytree(info.install_path, project.parent / "publisher")
    else:
        write_marker(info.install_path, locked.resolved_commit)
    plan = plan_native_claude_plugin(
        info,
        package_name=locked.get_unique_key(),
        targets=[KNOWN_TARGETS["claude"]],
        scope=InstallScope.USER,
        skill_subset=None,
        hooks_approved=True,
        skip_bin=True,
    )
    assert plan is not None
    lock = LockFile(dependencies={locked.get_unique_key(): locked})
    lock.write(project / "apm.lock.yaml")
    return modules, lock, plan


@pytest.mark.parametrize("changed_root", ["publisher", "cache"])
def test_local_lock_hash_and_admitted_cache_fingerprint_are_independent(
    tmp_path: Path, native, changed_root: str
) -> None:
    from apm_cli.install.phases.local_content import compute_local_package_hash
    from apm_cli.utils.content_hash import compute_package_hash

    modules, lock, plan = _locked_native_fixture(tmp_path / "project", native, local=True)
    publisher = tmp_path / "publisher"
    (publisher / "apm.yml").unlink()
    locked = lock.dependencies[plan.dependency_key]
    locked.content_hash = compute_local_package_hash(publisher)
    assert locked.content_hash != compute_package_hash(plan.source_root)
    kwargs = {
        "modules_dir": modules,
        "scope": InstallScope.USER,
        "lockfile": lock,
        "prepared": {plan.dependency_key: plan},
    }
    resync_claude_plugins(**kwargs)
    ledger = (modules / LEDGER_RELATIVE).read_bytes()
    native["calls"].clear()
    root = publisher if changed_root == "publisher" else plan.source_root
    (root / "resources/identity.json").write_text('{"name":"changed"}', encoding="ascii")
    expected = (
        "source integrity changed" if changed_root == "publisher" else "after deployment admission"
    )
    with pytest.raises(ClaudeNativePluginError, match=expected):
        resync_claude_plugins(**kwargs)
    assert (modules / LEDGER_RELATIVE).read_bytes() == ledger
    assert native["calls"] == []


def _change_native_resource(root: Path, change: str) -> None:
    """Change a publisher-controlled input excluded by the legacy lock digest."""
    if change == "cache-resource":
        (root / "__pycache__/publisher-resource.bin").write_bytes(b"changed owned resource\n")
    else:
        script = root / "hooks/probe.cjs"
        script.chmod(script.stat().st_mode | stat.S_IXUSR)


def _readmit_native(locked, root: Path) -> ClaudeNativePlugin:
    from apm_cli.claude_plugins.admission import plan_native_claude_plugin
    from apm_cli.integration.targets import KNOWN_TARGETS
    from apm_cli.models.apm_package import APMPackage, PackageInfo, PackageType

    plan = plan_native_claude_plugin(
        PackageInfo(
            package=APMPackage.from_apm_yml(root / "apm.yml"),
            install_path=root,
            package_type=PackageType(locked.package_type),
            dependency_ref=locked.to_dependency_ref(),
        ),
        package_name=locked.get_unique_key(),
        targets=[KNOWN_TARGETS["claude"]],
        scope=InstallScope.USER,
        skill_subset=None,
        hooks_approved=True,
        skip_bin=True,
    )
    assert plan is not None
    return plan


@pytest.mark.parametrize(
    "change",
    [
        "cache-resource",
        pytest.param(
            "executable",
            marks=pytest.mark.skipif(os.name == "nt", reason="POSIX owner-execute-bit witness"),
        ),
    ],
)
@pytest.mark.parametrize("boundary", ["prepared", "survivor", "readback", "audit", "readmit"])
def test_complete_native_bundle_drift_is_not_legacy_hash_equivalence(
    tmp_path: Path, native, monkeypatch, change: str, boundary: str
) -> None:
    """Exercise admission, ownership, metadata readback and replay with real files."""
    from apm_cli.agent_plugins.errors import AgentPluginDeploymentBoundaryError
    from apm_cli.install.drift import CheckLogger, ReplayConfig, run_replay
    from apm_cli.utils.content_hash import compute_package_hash

    project = tmp_path / "project"
    modules, lock, plan = _locked_native_fixture(project, native)
    kwargs = {"modules_dir": modules, "scope": InstallScope.USER, "lockfile": lock}
    prepared = {plan.dependency_key: plan}
    legacy_hash = compute_package_hash(plan.source_root)
    replay = ReplayConfig(
        project_root=project,
        lockfile_path=project / "apm.lock.yaml",
        targets=frozenset({"claude"}),
        user_scope=True,
        scratch_root=tmp_path / "scratch",
    )
    if boundary in {"survivor", "audit", "readmit"}:
        resync_claude_plugins(**kwargs, prepared=prepared)
    if boundary == "audit":
        run_replay(replay, CheckLogger(verbose=False))
    native["calls"].clear()
    if boundary == "readback":
        original = ClaudePluginCLI.run

        def mutate_after_native_write(self, *args):
            result = original(self, *args)
            if args[:2] == ("plugin", "install"):
                _change_native_resource(plan.source_root, change)
            return result

        monkeypatch.setattr(ClaudePluginCLI, "run", mutate_after_native_write)
    else:
        _change_native_resource(plan.source_root, change)
        assert compute_package_hash(plan.source_root) == legacy_hash
        if boundary == "readmit":
            locked = lock.dependencies[plan.dependency_key]
            fresh = _readmit_native(locked, plan.source_root)
            prepared = {plan.dependency_key: fresh}
    with pytest.raises(
        (ClaudeNativePluginError, AgentPluginDeploymentBoundaryError),
        match=r"fingerprint|integrity|changed",
    ):
        if boundary == "audit":
            run_replay(replay, CheckLogger(verbose=False))
        else:
            resync_claude_plugins(**kwargs, prepared=None if boundary == "survivor" else prepared)
    if boundary != "readback":
        assert native["calls"] == []
    assert compute_package_hash(plan.source_root) == legacy_hash


@pytest.mark.parametrize("local", [False, True])
@pytest.mark.parametrize(
    "change",
    [
        "unchanged",
        "cache-resource",
        pytest.param("executable", marks=pytest.mark.skipif(os.name == "nt", reason="POSIX mode")),
    ],
)
def test_native_fingerprint_allows_readmitted_updates_and_preserves_activation(
    tmp_path: Path, native, local: bool, change: str
) -> None:
    from apm_cli.install.cache_pin import write_marker

    modules, lock, plan = _locked_native_fixture(tmp_path / "project", native, local=local)
    kwargs = {"modules_dir": modules, "scope": InstallScope.USER, "lockfile": lock}
    resync_claude_plugins(**kwargs, prepared={plan.dependency_key: plan})
    plugin_id = f"{plan.name}@{MARKETPLACE_NAME}"
    native["plugins"][plugin_id]["enabled"] = True
    locked = lock.dependencies[plan.dependency_key]
    if change != "unchanged":
        _change_native_resource(plan.source_root, change)
        if local:
            _change_native_resource(tmp_path / "publisher", change)
        else:
            locked.resolved_commit = "b" * 40
            write_marker(plan.source_root, locked.resolved_commit)
    fresh = _readmit_native(locked, plan.source_root)
    resync_claude_plugins(**kwargs, prepared={plan.dependency_key: fresh})
    entry = json.loads((modules / LEDGER_RELATIVE).read_text())["plugins"][plugin_id]
    assert entry["bundle_hash"] == fresh.bundle_hash
    assert (entry["bundle_hash"] == plan.bundle_hash) == (change == "unchanged")
    assert entry["source_pin"] == f"{locked.source}:{locked.local_path or locked.resolved_commit}"
    assert native["plugins"][plugin_id]["enabled"] is True


def test_legacy_native_ownership_requires_explicit_removal_before_attestation(
    tmp_path: Path, native
) -> None:
    from apm_cli.deps.lockfile import LockFile

    modules, lock, plan = _locked_native_fixture(tmp_path / "project", native)
    kwargs = {"modules_dir": modules, "scope": InstallScope.USER, "lockfile": lock}
    resync_claude_plugins(**kwargs, prepared={plan.dependency_key: plan})
    ledger = modules / LEDGER_RELATIVE
    document = json.loads(ledger.read_text())
    document["version"] = 1
    for entry in document["plugins"].values():
        del entry["bundle_hash"], entry["source_pin"]
    ledger.write_text(json.dumps(document))
    before = ledger.read_bytes()
    native["calls"].clear()
    with pytest.raises(ClaudeNativePluginError, match="no complete native fingerprint"):
        resync_claude_plugins(**kwargs, prepared={plan.dependency_key: plan})
    assert ledger.read_bytes() == before
    assert native["calls"] == []
    shutil.rmtree(plan.source_root)
    native["fail_uninstall"] = True
    with pytest.raises(ClaudeNativePluginError, match="interrupted before native removal"):
        resync_claude_plugins(modules_dir=modules, scope=InstallScope.USER, lockfile=LockFile())
    interrupted = json.loads(ledger.read_text())
    assert interrupted["version"] == 2
    assert set(next(iter(interrupted["plugins"].values()))) == {"dependency_key", "source"}
    native["fail_uninstall"] = False
    resync_claude_plugins(modules_dir=modules, scope=InstallScope.USER, lockfile=LockFile())
    assert json.loads(ledger.read_text())["plugins"] == {}
    assert native["plugins"] == {}


@pytest.mark.parametrize("damage", ["fingerprint", "pin", "source", "missing-source"])
def test_native_ownership_damage_is_not_replayed_or_silently_retired(
    tmp_path: Path, native, damage: str
) -> None:
    from apm_cli.agent_plugins.errors import AgentPluginDeploymentBoundaryError
    from apm_cli.install.drift import CheckLogger, ReplayConfig, run_replay

    project = tmp_path / "project"
    modules, lock, plan = _locked_native_fixture(project, native)
    resync_claude_plugins(
        modules_dir=modules,
        scope=InstallScope.USER,
        lockfile=lock,
        prepared={plan.dependency_key: plan},
    )
    ledger = modules / LEDGER_RELATIVE
    document = json.loads(ledger.read_text())
    entry = next(iter(document["plugins"].values()))
    if damage == "missing-source":
        shutil.rmtree(plan.source_root)
    else:
        entry[{"fingerprint": "bundle_hash", "pin": "source_pin", "source": "source"}[damage]] = (
            "wrong"
        )
        ledger.write_text(json.dumps(document))
    before = ledger.read_bytes()
    native["calls"].clear()
    with pytest.raises(ClaudeNativePluginError, match=r"ledger|missing|changed"):
        resync_claude_plugins(modules_dir=modules, scope=InstallScope.USER, lockfile=lock)
    assert ledger.read_bytes() == before
    assert native["calls"] == []
    if damage != "missing-source":
        with pytest.raises(AgentPluginDeploymentBoundaryError, match=r"ledger|changed"):
            run_replay(
                ReplayConfig(
                    project_root=project,
                    lockfile_path=project / "apm.lock.yaml",
                    targets=frozenset({"claude"}),
                    user_scope=True,
                    scratch_root=tmp_path / "scratch",
                ),
                CheckLogger(verbose=False),
            )
        assert ledger.read_bytes() == before


@pytest.mark.parametrize("change_at", ["source", "cache"])
@pytest.mark.parametrize(
    "change",
    [
        "cache-resource",
        pytest.param("executable", marks=pytest.mark.skipif(os.name == "nt", reason="POSIX mode")),
    ],
)
def test_local_native_audit_checks_source_and_live_cache_without_repairing_either(
    tmp_path: Path, native, change_at: str, change: str
) -> None:
    from apm_cli.agent_plugins.errors import AgentPluginDeploymentBoundaryError
    from apm_cli.install.audit_target_roots import replay_modules_root
    from apm_cli.install.drift import CheckLogger, ReplayConfig, run_replay

    project = tmp_path / "project"
    modules, lock, plan = _locked_native_fixture(project, native, local=True)
    resync_claude_plugins(
        modules_dir=modules,
        scope=InstallScope.USER,
        lockfile=lock,
        prepared={plan.dependency_key: plan},
    )
    config = ReplayConfig(
        project_root=project,
        lockfile_path=project / "apm.lock.yaml",
        targets=frozenset({"claude"}),
        user_scope=True,
        scratch_root=tmp_path / "scratch",
    )
    run_replay(config, CheckLogger(verbose=False))
    assert not replay_modules_root(config.scratch_root, user_scope=True).exists()
    source = tmp_path / "publisher"
    _change_native_resource(source if change_at == "source" else plan.source_root, change)
    before = {root: fingerprint_native_claude_source(root) for root in (source, plan.source_root)}
    ledger_before = (modules / LEDGER_RELATIVE).read_bytes()
    native["calls"].clear()
    with pytest.raises(AgentPluginDeploymentBoundaryError, match="fingerprint changed"):
        run_replay(config, CheckLogger(verbose=False))
    assert {root: fingerprint_native_claude_source(root) for root in before} == before
    assert (modules / LEDGER_RELATIVE).read_bytes() == ledger_before
    assert native["calls"] == []


def test_cold_native_fingerprint_replay_keeps_the_saved_pin_and_never_registers(
    tmp_path: Path, native
) -> None:
    from apm_cli.agent_plugins.errors import AgentPluginDeploymentBoundaryError
    from apm_cli.claude_plugins.registrar import verify_claude_native_cache

    modules, lock, plan = _locked_native_fixture(tmp_path / "project", native)
    resync_claude_plugins(
        modules_dir=modules,
        scope=InstallScope.USER,
        lockfile=lock,
        prepared={plan.dependency_key: plan},
    )
    restored = tmp_path / "restored"
    shutil.copytree(plan.source_root, restored)
    shutil.rmtree(plan.source_root)
    native["calls"].clear()
    kwargs = {
        "modules_dir": modules,
        "locked": lock.dependencies[plan.dependency_key],
        "source_root": restored,
    }
    verify_claude_native_cache(**kwargs)
    _change_native_resource(restored, "cache-resource")
    with pytest.raises(AgentPluginDeploymentBoundaryError, match="fingerprint changed"):
        verify_claude_native_cache(**kwargs)
    assert not plan.source_root.exists()
    assert native["calls"] == []


def test_post_readback_mutation_retains_baseline_until_original_bytes_are_restored(
    tmp_path: Path, native
) -> None:
    modules, lock, plan = _locked_native_fixture(tmp_path / "project", native)
    kwargs = {"modules_dir": modules, "scope": InstallScope.USER, "lockfile": lock}
    native["fail"] = True
    with pytest.raises(ClaudeNativePluginError, match="interrupted"):
        resync_claude_plugins(**kwargs, prepared={plan.dependency_key: plan})
    ledger = modules / LEDGER_RELATIVE
    before = ledger.read_bytes()
    asset = plan.source_root / "__pycache__/publisher-resource.bin"
    original = asset.read_bytes()
    _change_native_resource(plan.source_root, "cache-resource")
    native["fail"] = False
    native["calls"].clear()
    with pytest.raises(ClaudeNativePluginError, match="fingerprint changed"):
        resync_claude_plugins(**kwargs)
    assert ledger.read_bytes() == before
    assert native["calls"] == []
    asset.write_bytes(original)
    resync_claude_plugins(**kwargs)
    assert ledger.read_bytes() == before
    assert native["plugins"][f"{plan.name}@{MARKETPLACE_NAME}"]["enabled"] is False


@pytest.mark.parametrize("boundary", ["locked", "prepared"])
def test_missing_native_source_pin_refuses_before_any_registration_write(
    tmp_path: Path, native, boundary: str
) -> None:
    from dataclasses import replace

    modules, lock, plan = _locked_native_fixture(tmp_path / "project", native)
    with pytest.raises(
        ClaudeNativePluginError, match=r"no immutable (pin|source identity)"
    ) as error:
        if boundary == "locked":
            lock.dependencies[plan.dependency_key].resolved_commit = None
            resync_claude_plugins(
                modules_dir=modules,
                scope=InstallScope.USER,
                lockfile=lock,
                prepared={plan.dependency_key: plan},
            )
        else:
            synchronize_claude_plugins(
                modules_dir=modules,
                scope=InstallScope.USER,
                plugins=[replace(plan, source_pin="")],
            )
    assert "Reinstall" in str(error.value)
    assert native["calls"] == []
    assert not (modules / LEDGER_RELATIVE).exists()
    assert not (modules / CATALOG_RELATIVE).exists()


def test_normalized_native_source_fingerprint_is_replay_idempotent(tmp_path: Path, native) -> None:
    from apm_cli.deps.plugin_parser import normalize_plugin_directory

    _, _, plan = _locked_native_fixture(tmp_path / "project", native)
    replay = tmp_path / "replay"
    shutil.copytree(plan.source_root, replay)
    before = (replay / "apm.yml").read_bytes()
    normalize_plugin_directory(replay, replay / ".claude-plugin/plugin.json")
    assert (replay / "apm.yml").read_bytes() == before
    assert fingerprint_native_claude_source(replay) == plan.bundle_hash


@pytest.mark.parametrize(
    "change",
    [
        "cache-resource",
        pytest.param("executable", marks=pytest.mark.skipif(os.name == "nt", reason="POSIX mode")),
    ],
)
def test_source_changed_after_admission_never_reaches_native_synchronization(
    tmp_path: Path, native, change: str
) -> None:
    from dataclasses import replace

    modules, lock, plan = _locked_native_fixture(tmp_path / "project", native)
    _change_native_resource(plan.source_root, change)
    with pytest.raises(ClaudeNativePluginError, match="fingerprint changed"):
        synchronize_claude_plugins(
            modules_dir=modules,
            scope=InstallScope.USER,
            plugins=[
                replace(
                    plan,
                    source_pin="github:" + lock.dependencies[plan.dependency_key].resolved_commit,
                )
            ],
        )
    assert native["calls"] == []
    assert not (modules / LEDGER_RELATIVE).exists()


def test_empty_native_ownership_never_requires_the_claude_client(
    tmp_path: Path, native, monkeypatch
) -> None:
    """After the last removal, unrelated global operations must not need Claude."""
    from apm_cli.deps.lockfile import LockFile

    _sync(tmp_path, ["alpha"])
    _sync(tmp_path, [])
    assert json.loads((tmp_path / LEDGER_RELATIVE).read_text())["plugins"] == {}

    def unavailable(self, *args):
        raise ClaudeNativePluginError("Claude is not on PATH")

    monkeypatch.setattr(ClaudePluginCLI, "run", unavailable)
    _sync(tmp_path, [])
    resync_claude_plugins(modules_dir=tmp_path, scope=InstallScope.USER, lockfile=None)
    resync_claude_plugins(modules_dir=tmp_path, scope=InstallScope.USER, lockfile=LockFile())


@pytest.mark.parametrize(
    ("declared", "retained"),
    [(None, True), (["claude", "codex"], True), (["codex"], False)],
)
def test_target_narrowing_retires_native_plugins_only_when_claude_is_undeclared(
    tmp_path: Path, native, declared: list[str] | None, retained: bool
) -> None:
    """A one-shot --target codex keeps declared Claude registrations and consent."""
    from apm_cli.integration.targets import KNOWN_TARGETS

    modules, lock, plan = _locked_native_fixture(tmp_path / "project", native)
    kwargs = {"modules_dir": modules, "scope": InstallScope.USER, "lockfile": lock}
    resync_claude_plugins(**kwargs, prepared={plan.dependency_key: plan})
    plugin_id = f"{plan.name}@{MARKETPLACE_NAME}"
    native["plugins"][plugin_id]["enabled"] = True
    resync_claude_plugins(
        **kwargs,
        targets=[KNOWN_TARGETS["codex"]],
        retained_targets=None if declared is None else [KNOWN_TARGETS[n] for n in declared],
        prepared={},
    )
    assert (plugin_id in native["plugins"]) is retained
    if retained:
        assert native["plugins"][plugin_id]["enabled"] is True
    assert all(call[1:2] not in (("enable",), ("disable",)) for call in native["calls"])
