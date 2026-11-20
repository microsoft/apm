"""Codex split install (#3920): one A3 diagnostic and retirement of legacy registrations."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import tomlkit

from apm_cli.adapters.client.codex import CodexClientAdapter
from apm_cli.codex_plugins.legacy_registrations import (
    legacy_ledger_path,
    report_legacy_retirement,
    retire_legacy_codex_registrations,
)
from apm_cli.core.scope import InstallScope
from apm_cli.install.phases import codex_plugins as phase
from apm_cli.install.target_warnings import note_codex_split_install
from apm_cli.models.apm_package import PackageType
from apm_cli.utils.diagnostics import CATEGORY_INFO, DiagnosticCollector


def _target(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name)


def _package(name: str, package_type: PackageType) -> SimpleNamespace:
    return SimpleNamespace(
        package=SimpleNamespace(name=name), package_type=package_type, install_path="/x/" + name
    )


def _ctx(**values: object) -> SimpleNamespace:
    defaults = {
        "codex_split_plugins": set(),
        "dry_run": False,
        "lockfile_only": False,
        "apm_modules_dir": None,
        "scope": InstallScope.USER,
        "diagnostics": DiagnosticCollector(),
        "logger": MagicMock(),
    }
    return SimpleNamespace(**{**defaults, **values})


@pytest.mark.parametrize(
    ("package_type", "targets", "recorded"),
    [
        (PackageType.MARKETPLACE_PLUGIN, ["claude", "codex"], True),
        (PackageType.MARKETPLACE_PLUGIN, ["claude"], False),
        (PackageType.APM_PACKAGE, ["codex"], False),
        (PackageType.AGENT_PLUGIN, ["codex"], False),
    ],
)
def test_only_plugins_deployed_to_codex_are_recorded(package_type, targets, recorded) -> None:
    ctx = _ctx()
    note_codex_split_install(
        _package("demo", package_type), [_target(name) for name in targets], ctx
    )
    assert ctx.codex_split_plugins == ({"demo"} if recorded else set())


@pytest.mark.parametrize(
    ("scope", "label"), [(InstallScope.USER, "user"), (InstallScope.PROJECT, "project")]
)
def test_phase_reports_one_diagnostic_naming_target_scope_plugins_and_a3(scope, label) -> None:
    ctx = _ctx(codex_split_plugins={"zeta", "alpha"}, scope=scope)
    phase.run(ctx)
    infos = ctx.diagnostics.by_category()[CATEGORY_INFO]
    assert len(infos) == 1
    message = infos[0].message
    assert message.startswith("Codex: split-installed 2 plugin(s) at " + label + " scope")
    assert "(alpha, zeta)" in message
    assert "A3" in message
    assert message.isascii()


@pytest.mark.parametrize("flag", ["dry_run", "lockfile_only"])
def test_phase_is_silent_without_deployment(flag) -> None:
    ctx = _ctx(codex_split_plugins={"demo"}, **{flag: True})
    phase.run(ctx)
    assert not ctx.diagnostics.has_diagnostics


def test_dry_run_wording_reports_the_same_decision() -> None:
    message = phase.split_install_message({"demo"}, InstallScope.USER, dry_run=True)
    assert message.startswith("Codex: would split-install 1 plugin(s) at user scope (demo)")
    assert "A3" in message


# -- legacy retirement ------------------------------------------------------


def _marketplace(modules: Path, key: str) -> str:
    """The name the removed app-server route derived for an APM home and dependency."""
    identity = f"{modules.resolve().as_posix()}\0{key}".encode()
    return "apm-" + hashlib.sha256(identity).hexdigest()[:24]


@pytest.fixture
def legacy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A registration exactly as the app-server route left it, plus user entries."""
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    modules = tmp_path / "apm" / "apm_modules"
    ledger = legacy_ledger_path(modules)
    snapshot = ledger.parent / "snapshots" / "abc"
    snapshot.mkdir(parents=True)
    (snapshot / "plugin.json").write_text("{}", encoding="utf-8")
    key = "owner/example"
    marketplace = _marketplace(modules, key)
    config = codex_home / "config.toml"
    ledger.write_text(
        json.dumps(
            {
                "version": 1,
                "plugins": {
                    key: {
                        "name": "example",
                        "snapshot": {
                            "path": str(snapshot),
                            "content_commit": "a" * 40,
                            "catalog_commit": "b" * 40,
                        },
                        "previous": None,
                        "config_path": str(config),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    config.write_text(
        'model = "gpt-5.4"\n\n'
        f'[plugins."example@{marketplace}"]\nenabled = false\n\n'
        '[plugins."foreign@external"]\nenabled = true\n\n'
        f"[marketplaces.{marketplace}]\n"
        f'source_type = "git"\nsource = "{snapshot.as_uri()}"\nref = "{"b" * 40}"\n\n'
        '[marketplaces.external]\nsource_type = "local"\nsource = "/elsewhere"\n',
        encoding="utf-8",
    )
    return SimpleNamespace(
        modules=modules, ledger=ledger, snapshot=snapshot, config=config, marketplace=marketplace
    )


def _config(path: Path) -> dict:
    return tomlkit.parse(path.read_text(encoding="utf-8")).unwrap()


def test_retires_owned_registration_and_keeps_user_entries(legacy) -> None:
    outcome = retire_legacy_codex_registrations(legacy.modules, InstallScope.USER)
    assert outcome is not None
    assert (outcome.retired, outcome.kept, outcome.problem) == (("example",), (), None)
    config = _config(legacy.config)
    assert config["model"] == "gpt-5.4"
    assert config["plugins"] == {"foreign@external": {"enabled": True}}
    assert list(config["marketplaces"]) == ["external"]
    assert not legacy.snapshot.exists()
    assert not legacy.ledger.parent.exists()
    assert retire_legacy_codex_registrations(legacy.modules, InstallScope.USER) is None


@pytest.mark.parametrize(
    "edit",
    [
        lambda text, m: text.replace("enabled = false", "enabled = true", 1),
        lambda text, m: text.replace('ref = "' + "b" * 40, 'ref = "' + "c" * 40),
        lambda text, m: text + f'\n[plugins."other@{m}"]\nenabled = false\n',
    ],
    ids=["user-enabled", "source-changed", "shared-marketplace"],
)
def test_changed_registration_is_left_to_the_user(legacy, edit) -> None:
    legacy.config.write_text(
        edit(legacy.config.read_text(encoding="utf-8"), legacy.marketplace), encoding="utf-8"
    )
    before = legacy.config.read_bytes()
    outcome = retire_legacy_codex_registrations(legacy.modules, InstallScope.USER)
    assert (outcome.retired, outcome.kept) == ((), ("example",))
    assert legacy.config.read_bytes() == before
    assert legacy.snapshot.is_dir()
    assert not legacy.ledger.exists()


def test_registration_in_another_codex_home_is_left_alone(legacy, monkeypatch, tmp_path) -> None:
    other = tmp_path / "other-codex"
    other.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(other))
    before = legacy.config.read_bytes()
    outcome = retire_legacy_codex_registrations(legacy.modules, InstallScope.USER)
    assert outcome.kept == ("example",)
    assert legacy.config.read_bytes() == before
    assert not (other / "config.toml").exists()
    assert legacy.snapshot.is_dir()


@pytest.mark.parametrize("target", ["ledger", "config"])
def test_unreadable_state_changes_nothing_and_keeps_the_journal(legacy, target) -> None:
    broken = legacy.ledger if target == "ledger" else legacy.config
    broken.write_text("{not valid", encoding="utf-8")
    before = {path: path.read_bytes() for path in (legacy.ledger, legacy.config)}
    outcome = retire_legacy_codex_registrations(legacy.modules, InstallScope.USER)
    assert outcome.problem
    assert before == {path: path.read_bytes() for path in before}
    assert legacy.snapshot.is_dir()


def test_project_scope_never_reads_the_user_journal(legacy) -> None:
    assert retire_legacy_codex_registrations(legacy.modules, InstallScope.PROJECT) is None
    assert legacy.ledger.is_file()


def test_report_names_retired_kept_and_problems() -> None:
    from apm_cli.codex_plugins.legacy_registrations import LegacyRetirement

    logger = MagicMock()
    report_legacy_retirement(LegacyRetirement(retired=("a",), kept=("b",)), logger)
    assert "Retired 1 Codex plugin registration(s)" in logger.info.call_args.args[0]
    assert "b" in logger.warning.call_args.args[0]
    logger = MagicMock()
    report_legacy_retirement(LegacyRetirement(problem="cannot parse x"), logger)
    assert "nothing was changed" in logger.warning.call_args.args[0]
    logger.info.assert_not_called()


def test_adapter_unparseable_config_returns_none(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text("[broken", encoding="utf-8")
    assert CodexClientAdapter(user_scope=True).retire_plugin_registrations({"m": ("p", [])}) is None


@pytest.mark.parametrize(
    "corrupt",
    ["relative-snapshot", "plugins-not-a-table", "write-fails"],
)
def test_unexpected_state_is_reported_never_raised(legacy, monkeypatch, corrupt) -> None:
    """Migration runs after deployment: a failure must not fail install (#3920)."""
    if corrupt == "relative-snapshot":
        data = json.loads(legacy.ledger.read_text(encoding="utf-8"))
        data["plugins"]["owner/example"]["snapshot"]["path"] = "relative"
        legacy.ledger.write_text(json.dumps(data), encoding="utf-8")
    elif corrupt == "plugins-not-a-table":
        legacy.config.write_text('plugins = "flat"\n', encoding="utf-8")
    else:

        def refuse(self, config):
            raise OSError("read-only file system")

        monkeypatch.setattr(CodexClientAdapter, "_write_config", refuse)
    outcome = retire_legacy_codex_registrations(legacy.modules, InstallScope.USER)
    if corrupt == "relative-snapshot":
        assert outcome.kept == ("example",)
    else:
        assert outcome.problem
        assert legacy.ledger.is_file()


def test_dry_run_reports_an_already_normalized_plugin(tmp_path, monkeypatch) -> None:
    """Dry-run types cached plugins like the real install, after normalization."""
    from apm_cli.deps.lockfile import LockedDependency, LockFile
    from apm_cli.models.apm_package import DependencyReference
    from apm_cli.models.validation import validate_apm_package

    monkeypatch.chdir(tmp_path)
    reference = DependencyReference.parse("owner/example")
    package = reference.get_install_path(tmp_path / "apm_modules")
    (package / ".codex-plugin").mkdir(parents=True)
    (package / ".codex-plugin/plugin.json").write_text('{"name": "example"}', encoding="utf-8")
    skill = package / "skills/example/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: example\ndescription: d\n---\n", encoding="utf-8")
    assert validate_apm_package(package).is_valid
    locked = LockedDependency(
        repo_url="owner/example", name="example", package_type="marketplace_plugin"
    )
    LockFile(dependencies={locked.get_unique_key(): locked}).write(tmp_path / "apm.lock.yaml")
    logger = MagicMock()
    ctx = SimpleNamespace(
        project_root=tmp_path,
        apm_dir=tmp_path,
        scope=InstallScope.PROJECT,
        target="codex",
        targets=[],
        logger=logger,
    )
    phase.report_codex_split_dry_run(ctx, [reference], apm_package=None)
    message = logger.info.call_args.args[0]
    assert message.startswith("Codex: would split-install 1 plugin(s) at project scope (example)")
