"""Project-scope native plugins for Claude Code and GitHub Copilot (#3921).

APM deploys approved roots to the committed ``.agents/plugins``, declares them
with a relative path in ``.claude/settings.json`` and
``.github/copilot/settings.json`` per the plugin's harnesses, and owns only
what the previous and next lock produced.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from apm_cli.claude_plugins.admission import (
    ClaudeNativePlugin,
    ClaudeNativePluginError,
    fingerprint_native_claude_source,
)
from apm_cli.claude_plugins.registrar import (
    native_ownership,
    reconcile_native_plugins,
    scope_binding,
    verify_claude_native_cache,
)
from apm_cli.core.scope import InstallScope
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.native_plugins import scopes
from apm_cli.native_plugins.record import NativePluginRecord
from apm_cli.native_plugins.scopes import (
    CLAUDE,
    COPILOT,
    PROJECT_MARKETPLACE_PATH,
    project_marketplace_name,
)

_CLAUDE = ".claude/settings.json"
_COPILOT = ".github/copilot/settings.json"


@pytest.fixture(autouse=True)
def _identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scopes, "project_identity", lambda root: "github.com/acme/app")


class _Project:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.modules = root / "apm_modules"
        self.plugins = root / ".agents/plugins"
        self.lock = LockFile()
        self.prepared: dict[str, ClaudeNativePlugin] = {}

    @property
    def marketplace(self) -> str:
        return project_marketplace_name(self.root, None, None)

    def source(self, repo: str, name: str, *, harnesses=(CLAUDE, COPILOT), body="v1") -> Path:
        root = self.modules / repo
        (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        (root / ".claude-plugin/plugin.json").write_text(json.dumps({"name": name}))
        (root / "hooks").mkdir(exist_ok=True)
        script = root / "hooks/run.sh"
        script.write_text(body)
        if os.name != "nt":
            script.chmod(0o755)
        fingerprint = fingerprint_native_claude_source(root, portable=True)
        dependency = LockedDependency(
            repo_url=repo, resolved_commit="a" * 40, package_type="marketplace_plugin"
        )
        if set(harnesses) != {CLAUDE, COPILOT}:
            dependency.target_subset = sorted(harnesses)
        dependency.native_plugin = NativePluginRecord(
            name, fingerprint, self.marketplace, PROJECT_MARKETPLACE_PATH
        )
        self.lock.dependencies[repo] = dependency
        self.prepared[repo] = ClaudeNativePlugin(
            repo, name, root.resolve(), fingerprint, harnesses=frozenset(harnesses)
        )
        return root

    def reconcile(
        self, previous, *, run_targets=(CLAUDE, COPILOT), prepared=True, logger=None
    ) -> None:
        reconcile_native_plugins(
            modules_dir=self.modules,
            scope=InstallScope.PROJECT,
            previous=previous,
            lockfile=self.lock,
            prepared=self.prepared if prepared else None,
            deploy_root=self.root,
            run_targets=None if run_targets is None else frozenset(run_targets),
            logger=logger,
        )

    def settings(self, relative: str) -> dict:
        path = self.root / relative
        return json.loads(path.read_text()) if path.exists() else {}


@pytest.fixture
def project(tmp_path: Path) -> _Project:
    return _Project(tmp_path / "repo")


def test_install_commits_a_relative_declaration_to_both_harness_files(project) -> None:
    project.source("acme/alpha", "alpha")
    project.reconcile(None)
    expected = {
        "extraKnownMarketplaces": {
            project.marketplace: {"source": {"source": "directory", "path": "./.agents/plugins"}}
        },
        "enabledPlugins": {f"alpha@{project.marketplace}": True},
    }
    assert project.settings(_CLAUDE) == expected
    assert project.settings(_COPILOT) == expected
    catalog = json.loads((project.plugins / ".claude-plugin/marketplace.json").read_text())
    assert catalog["name"] == project.marketplace
    assert catalog["plugins"] == [{"name": "alpha", "source": "./alpha"}]
    assert (project.plugins / "alpha/hooks/run.sh").read_text() == "v1"
    # The per-machine exec bit travels with the deployed copy.
    if os.name != "nt":
        assert os.access(project.plugins / "alpha/hooks/run.sh", os.X_OK)


def test_a_claude_only_plugin_is_switched_off_for_copilot_by_apm(project) -> None:
    """Copilot also reads .claude/settings.json; APM's false in its own file wins."""
    project.source("acme/alpha", "alpha", harnesses=(CLAUDE,))
    project.reconcile(None)
    key = f"alpha@{project.marketplace}"
    assert project.settings(_CLAUDE)["enabledPlugins"] == {key: True}
    assert project.settings(_COPILOT) == {"enabledPlugins": {key: False}}


def test_apm_owned_suppression_follows_the_targets_both_ways(project) -> None:
    key = f"alpha@{project.marketplace}"
    project.source("acme/alpha", "alpha", harnesses=(CLAUDE,))
    project.reconcile(None)
    previous = native_ownership(project.lock)
    project.source("acme/alpha", "alpha")
    project.reconcile(previous)
    assert project.settings(_COPILOT)["enabledPlugins"] == {key: True}
    previous = native_ownership(project.lock)
    project.source("acme/alpha", "alpha", harnesses=(CLAUDE,))
    project.reconcile(previous)
    assert project.settings(_COPILOT)["enabledPlugins"] == {key: False}
    previous = native_ownership(project.lock)
    project.lock.dependencies.clear()
    project.reconcile(previous, prepared=False)
    assert project.settings(_COPILOT)["enabledPlugins"] == {}


def test_a_run_that_did_not_evaluate_a_harness_keeps_its_declaration(project) -> None:
    project.source("acme/alpha", "alpha")
    project.reconcile(None)
    previous = native_ownership(project.lock)
    project.source("acme/alpha", "alpha", harnesses=(CLAUDE,))
    project.reconcile(previous, run_targets=(CLAUDE,))
    assert project.settings(_COPILOT)["enabledPlugins"] == {f"alpha@{project.marketplace}": True}
    project.reconcile(previous, run_targets=(CLAUDE, COPILOT))
    assert project.settings(_COPILOT) == {
        "extraKnownMarketplaces": {},
        "enabledPlugins": {f"alpha@{project.marketplace}": False},
    }


def test_removal_keeps_user_entries_and_explicit_false(project) -> None:
    project.source("acme/alpha", "alpha")
    project.source("acme/beta", "beta", harnesses=(COPILOT,))
    project.reconcile(None)
    copilot = project.root / _COPILOT
    document = project.settings(_COPILOT)
    document["enabledPlugins"][f"beta@{project.marketplace}"] = False
    document["enabledPlugins"]["mine@team"] = True
    document["extraKnownMarketplaces"]["team"] = {"source": {"source": "github", "repo": "a/b"}}
    copilot.write_text(json.dumps(document))
    claude = project.root / _CLAUDE
    document = project.settings(_CLAUDE)
    document["enabledPlugins"][f"alpha@{project.marketplace}"] = False
    claude.write_text(json.dumps(document))
    previous = native_ownership(project.lock)
    project.lock.dependencies.clear()
    project.reconcile(previous, prepared=False)
    assert project.settings(_COPILOT) == {
        "extraKnownMarketplaces": {"team": {"source": {"source": "github", "repo": "a/b"}}},
        "enabledPlugins": {f"beta@{project.marketplace}": False, "mine@team": True},
    }
    assert project.settings(_CLAUDE) == {
        "extraKnownMarketplaces": {},
        "enabledPlugins": {f"alpha@{project.marketplace}": False},
    }
    assert not project.plugins.exists()


def test_an_unparseable_second_file_leaves_every_file_and_root_untouched(project) -> None:
    project.source("acme/alpha", "alpha")
    broken = project.root / _COPILOT
    broken.parent.mkdir(parents=True)
    broken.write_text("{broken")
    with pytest.raises(ClaudeNativePluginError, match="Cannot read"):
        project.reconcile(None)
    assert broken.read_text() == "{broken"
    assert not (project.root / _CLAUDE).exists()
    assert not project.plugins.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX exec bit")
def test_committed_digest_ignores_the_exec_bit_a_teammate_checkout_drops(project) -> None:
    source = project.source("acme/alpha", "alpha")
    project.reconcile(None)
    locked = project.lock.dependencies["acme/alpha"]
    (project.plugins / "alpha/hooks/run.sh").chmod(0o644)
    verify_claude_native_cache(
        modules_dir=project.modules, locked=locked, source_root=source, project_root=project.root
    )
    (project.plugins / "alpha/hooks/run.sh").write_text("tampered")
    with pytest.raises(Exception, match="deployed copy differs"):
        verify_claude_native_cache(
            modules_dir=project.modules,
            locked=locked,
            source_root=source,
            project_root=project.root,
        )


def test_marketplace_name_is_per_project_and_stable_across_clones(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(scopes, "project_identity", lambda root: "github.com/acme/app")
    one = project_marketplace_name(tmp_path / "clone-one", None, None)
    assert one == project_marketplace_name(tmp_path / "elsewhere/clone-two", None, None)
    assert one.startswith("apm-p-")
    monkeypatch.setattr(scopes, "project_identity", lambda root: "github.com/acme/other")
    assert project_marketplace_name(tmp_path / "clone-one", None, None) != one
    # A fork keeps the name its committed lock or catalog already records.
    assert project_marketplace_name(tmp_path, one, None) == one
    assert project_marketplace_name(tmp_path, None, one) == one


def test_scope_binding_reuses_the_recorded_project_name(project) -> None:
    project.source("acme/alpha", "alpha")
    record = project.lock.dependencies["acme/alpha"].native_plugin
    renamed = NativePluginRecord(
        "alpha", record.fingerprint, "apm-p-0000abcd", record.marketplace_path
    )
    project.lock.dependencies["acme/alpha"].native_plugin = renamed
    assert scope_binding(
        user_scope=False,
        modules_dir=project.modules,
        deploy_root=project.root,
        previous=native_ownership(project.lock),
    ) == ("apm-p-0000abcd", PROJECT_MARKETPLACE_PATH)


_LOCAL = ".github/copilot/settings.local.json"
_LEGACY = {
    "model": "gpt",
    "extraKnownMarketplaces": {
        "apm": {"source": {"source": "directory", "path": "apm_modules"}},
        "team": {"source": {"source": "github", "repo": "a/b"}},
    },
    "enabledPlugins": {"alpha@apm": True, "off@apm": False, "mine@team": True},
}


def _legacy(project) -> tuple[Path, Path]:
    ledger = project.modules / ".github/plugin/apm-registration.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({"settingsPath": _LOCAL, "enabledPlugins": ["alpha@apm"]}))
    (project.modules / ".github/plugin/marketplace.json").write_text("{}")
    local = project.root / _LOCAL
    local.parent.mkdir(parents=True)
    local.write_text(json.dumps(_LEGACY))
    return ledger, local


def test_legacy_registration_moves_when_copilot_declares_its_plugins(project) -> None:
    project.source("acme/alpha", "alpha")
    _ledger, local = _legacy(project)
    project.reconcile(None)
    assert json.loads(local.read_text()) == {
        "model": "gpt",
        "extraKnownMarketplaces": {"team": {"source": {"source": "github", "repo": "a/b"}}},
        "enabledPlugins": {"off@apm": False, "mine@team": True},
    }
    assert not (project.modules / ".github").exists()
    assert f"alpha@{project.marketplace}" in project.settings(_COPILOT)["enabledPlugins"]


def test_a_failure_after_retirement_restores_the_legacy_registration(project) -> None:
    project.source("acme/alpha", "alpha")
    ledger, local = _legacy(project)
    before = local.read_bytes()
    foreign = project.plugins / ".claude-plugin/marketplace.json"
    foreign.parent.mkdir(parents=True)
    foreign.write_text('{"name": "someone-else"}')
    with pytest.raises(ClaudeNativePluginError):
        project.reconcile(None)
    assert local.read_bytes() == before
    assert ledger.is_file()
    assert not (project.root / _COPILOT).exists()


@pytest.mark.parametrize(
    ("run_targets", "harnesses"),
    [(None, (CLAUDE, COPILOT)), ((CLAUDE,), (CLAUDE,)), ((CLAUDE, COPILOT), (CLAUDE,))],
    ids=["uninstall-or-prune", "target-claude", "plugin-no-longer-for-copilot"],
)
def test_legacy_registration_is_kept_unless_copilot_declares_its_plugins(
    project, run_targets, harnesses
) -> None:
    project.source("acme/alpha", "alpha", harnesses=harnesses)
    ledger, local = _legacy(project)
    logger = MagicMock()
    project.reconcile(None, run_targets=run_targets, logger=logger)
    assert json.loads(local.read_text()) == _LEGACY
    assert ledger.is_file()
    warnings = " ".join(str(call.args[0]) for call in logger.warning.call_args_list)
    assert "still loads alpha" in warnings
    assert "apm install" in warnings


def test_an_unreadable_ledger_is_never_deleted(project) -> None:
    project.source("acme/alpha", "alpha")
    ledger, local = _legacy(project)
    ledger.write_text("{broken")
    project.reconcile(None)
    assert ledger.read_text() == "{broken"
    assert json.loads(local.read_text()) == _LEGACY


def test_legacy_marketplace_pointing_elsewhere_is_not_adopted(project) -> None:
    project.source("acme/alpha", "alpha")
    local = project.root / _LOCAL
    local.parent.mkdir(parents=True)
    foreign = {
        "extraKnownMarketplaces": {"apm": {"source": {"source": "directory", "path": "vendor"}}},
        "enabledPlugins": {"x@apm": True},
    }
    local.write_text(json.dumps(foreign))
    project.reconcile(None)
    assert json.loads(local.read_text()) == foreign


def _cli_project(tmp_path: Path, plugins: dict[str, list[str]]) -> Path:
    """A project with local Claude-format plugin dependencies and their ``targets:``."""
    project = tmp_path / "cli"
    entries = []
    for name, targets in plugins.items():
        source = tmp_path / "sources" / name
        (source / ".claude-plugin").mkdir(parents=True)
        (source / ".claude-plugin/plugin.json").write_text(json.dumps({"name": name}))
        (source / "skills/hello").mkdir(parents=True)
        (source / "skills/hello/SKILL.md").write_text("---\nname: hello\ndescription: hi\n---\n")
        entries.append({"path": str(source), "targets": targets})
    project.mkdir()
    (project / "apm.yml").write_text(
        json.dumps(
            {
                "name": "cli",
                "version": "1.0.0",
                "targets": ["claude", "copilot"],
                "dependencies": {"apm": entries},
            }
        )
    )
    return project


def _apm_install(monkeypatch: pytest.MonkeyPatch, project: Path, *args: str) -> str:
    """Run ``apm install`` in its own process: in-process runs reuse the apm.yml cache."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "apm_cli.cli", "install", "--no-policy", *args],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    return " ".join(output.split())


def _set_targets(project: Path, targets: list[str]) -> None:
    manifest = json.loads((project / "apm.yml").read_text())
    manifest["dependencies"]["apm"][0]["targets"] = targets
    (project / "apm.yml").write_text(json.dumps(manifest))


def _native_state(project: Path) -> dict[str, object]:
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path

    lock = LockFile.read(get_lockfile_path(project))
    files = sorted(
        (path.relative_to(project).as_posix(), path.read_bytes())
        for path in (project / ".agents").rglob("*")
        if path.is_file()
    )
    return {
        "claude": (project / _CLAUDE).read_bytes() if (project / _CLAUDE).exists() else None,
        "copilot": (project / _COPILOT).read_bytes(),
        "roots": files,
        "rows": {key: dep.to_dict() for key, dep in lock.dependencies.items()},
    }


@pytest.mark.parametrize("changed", [False, True], ids=["same-content", "changed-content"])
@pytest.mark.parametrize(
    ("targets", "narrowed"),
    [(["claude"], "copilot"), (["copilot"], "claude")],
    ids=["claude-only-plugin-target-copilot", "copilot-only-plugin-target-claude"],
)
def test_a_narrowed_target_never_retires_a_plugin_it_did_not_evaluate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, targets: list[str], narrowed: str, changed
) -> None:
    project = _cli_project(tmp_path, {"alpha": targets})
    _apm_install(monkeypatch, project)
    before = _native_state(project)
    assert any("native_plugin" in row for row in before["rows"].values())
    if changed:
        (tmp_path / "sources/alpha/skills/hello/SKILL.md").write_text("changed upstream\n")
    output = _apm_install(monkeypatch, project, "--target", narrowed)
    # The whole previous lock row, the deployed root and both declarations stay.
    assert _native_state(project) == before
    if changed:
        assert "Kept the previous version of native plugin 'alpha'" in output


@pytest.mark.parametrize(
    ("targets", "user_value", "restored"),
    [(["claude", "copilot"], False, True), (["claude"], True, False)],
    ids=["user-false-on-both", "user-true-on-claude-only"],
)
def test_apm_reports_when_it_flips_a_copilot_value_it_owns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    targets: list[str],
    user_value: bool,
    restored: bool,
) -> None:
    project = _cli_project(tmp_path, {"alpha": targets})
    _apm_install(monkeypatch, project)
    copilot = project / _COPILOT
    document = json.loads(copilot.read_text())
    (key,) = [key for key in document["enabledPlugins"] if key.startswith("alpha@")]
    document["enabledPlugins"][key] = user_value
    copilot.write_text(json.dumps(document))
    output = _apm_install(monkeypatch, project)
    assert json.loads(copilot.read_text())["enabledPlugins"][key] is restored
    assert f"[!] Set {key} to {str(restored).lower()}" in output
    assert "'targets:' in apm.yml" in output
    # Said once: the value now matches what APM writes, so a rerun is quiet.
    assert f"Set {key}" not in _apm_install(monkeypatch, project)


def test_a_value_apm_wrote_under_earlier_targets_changes_without_a_warning(project) -> None:
    project.source("acme/alpha", "alpha", harnesses=(CLAUDE,))
    project.reconcile(None)
    previous = native_ownership(project.lock)
    project.source("acme/alpha", "alpha")
    logger = MagicMock()
    project.reconcile(previous, logger=logger)
    key = f"alpha@{project.marketplace}"
    assert project.settings(_COPILOT)["enabledPlugins"][key] is True
    assert not [
        call for call in logger.warning.call_args_list if str(call.args[0]).startswith("Set ")
    ]


@pytest.mark.parametrize(
    ("before", "after", "copilot"),
    [(["claude", "copilot"], ["claude"], False), (["claude"], ["claude", "copilot"], True)],
    ids=["narrow-to-claude", "widen-to-copilot"],
)
def test_a_targets_edit_retires_the_stale_declaration_quietly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, before, after, copilot: bool
) -> None:
    """A value APM wrote under the previous targets changes without a [!] line."""
    project = _cli_project(tmp_path, {"alpha": before})
    _apm_install(monkeypatch, project)
    _set_targets(project, after)
    output = _apm_install(monkeypatch, project)
    enabled = json.loads((project / _COPILOT).read_text())["enabledPlugins"]
    (key,) = [key for key in enabled if key.startswith("alpha@")]
    assert enabled[key] is copilot
    assert json.loads((project / _CLAUDE).read_text())["enabledPlugins"][key] is True
    assert f"Set {key}" not in output


def _apm(project: Path, *args: str) -> tuple[int, str]:
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "apm_cli.cli", *args],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    return result.returncode, " ".join((result.stdout + result.stderr).split())


def test_a_narrowed_run_leaves_apms_copilot_value_to_change_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """targets narrowed, `--target claude`, then a full install: no [!] line."""
    project = _cli_project(tmp_path, {"alpha": ["claude", "copilot"]})
    _apm_install(monkeypatch, project)
    _set_targets(project, ["claude"])
    narrowed = _apm_install(monkeypatch, project, "--target", "claude")
    full = _apm_install(monkeypatch, project)
    enabled = json.loads((project / _COPILOT).read_text())["enabledPlugins"]
    (key,) = [key for key in enabled if key.startswith("alpha@")]
    assert enabled[key] is False
    assert "[!] Set" not in narrowed + full


def test_a_kept_plugin_deploys_nothing_loose_so_lock_and_disk_agree(tmp_path: Path) -> None:
    """targets [claude, cursor], source edited, `--target cursor` keeps the previous version."""
    source = tmp_path / "sources" / "alpha"
    (source / ".claude-plugin").mkdir(parents=True)
    (source / ".claude-plugin/plugin.json").write_text(json.dumps({"name": "alpha"}))
    skill = source / "skills/hello/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: hello\ndescription: hi\n---\nv1\n")
    project = tmp_path / "cli"
    project.mkdir()
    (project / "apm.yml").write_text(
        json.dumps(
            {
                "name": "cli",
                "version": "1.0.0",
                "targets": ["claude", "cursor"],
                "dependencies": {"apm": [str(source)]},
            }
        )
    )
    assert _apm(project, "install", "--no-policy")[0] == 0
    before = _native_state(project)
    loose = project / ".agents/skills/hello/SKILL.md"
    assert loose.read_text().endswith("v1\n")
    skill.write_text("---\nname: hello\ndescription: hi\n---\nv2\n")
    code, output = _apm(project, "install", "--no-policy", "--target", "cursor")
    assert code == 0, output
    assert "Kept the previous version of native plugin 'alpha'" in output
    assert loose.read_text().endswith("v1\n")
    assert _native_state(project) == before
    # The kept row still describes every file on disk; only the deferred
    # update (source newer than the lock) is reported, never deployed drift.
    code, output = _apm(project, "audit", "--ci")
    assert "deployed files missing" not in output
    assert "source differs from apm.lock.yaml" in output
    assert _apm(project, "install", "--no-policy")[0] == 0
    assert loose.read_text().endswith("v2\n")
    assert (project / ".agents/plugins/alpha/skills/hello/SKILL.md").read_text().endswith("v2\n")
    assert _apm(project, "audit", "--ci")[0] == 0
    assert _apm(project, "install", "--no-policy", "--frozen")[0] == 0


def test_a_kept_plugin_converges_on_the_next_full_install(tmp_path: Path) -> None:
    """Per-dependency targets [claude, cursor]; `.agents/skills` is shared with copilot."""
    project = _cli_project(tmp_path, {"alpha": ["claude", "cursor"]})
    manifest = json.loads((project / "apm.yml").read_text())
    manifest["targets"] = ["claude", "copilot", "cursor"]
    (project / "apm.yml").write_text(json.dumps(manifest))
    skill = tmp_path / "sources/alpha/skills/hello/SKILL.md"
    skill.write_text("---\nname: hello\ndescription: hi\n---\nv1\n")
    assert _apm(project, "install", "--no-policy")[0] == 0
    lock = (project / "apm.lock.yaml").read_bytes()
    skill.write_text("---\nname: hello\ndescription: hi\n---\nv2\n")
    assert _apm(project, "install", "--no-policy", "--target", "cursor")[0] == 0
    # The kept dependency's deployment records are not re-attributed to cursor.
    assert (project / "apm.lock.yaml").read_bytes() == lock
    code, output = _apm(project, "install", "--no-policy")
    assert code == 0, output
    for deployed in (
        ".agents/skills/hello/SKILL.md",
        ".agents/plugins/alpha/skills/hello/SKILL.md",
    ):
        assert (project / deployed).read_text().endswith("v2\n")
    assert _apm(project, "install", "--no-policy", "--frozen")[0] == 0
    assert "no drift detected" in _apm(project, "audit", "--ci")[1]


def test_an_evaluated_dependency_wins_a_ledger_record_it_shares_with_a_kept_one(
    tmp_path: Path,
) -> None:
    """Kept rows seed the ledger first, whatever the dependency order (#3921)."""
    from types import SimpleNamespace

    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
    from apm_cli.install.phases.lockfile import LockfileBuilder

    shared = ".claude/skills/shared/SKILL.md"
    (tmp_path / shared).parent.mkdir(parents=True)
    (tmp_path / shared).write_text("shared\n")
    existing = LockFile.from_yaml(
        "lockfile_version: '1'\n"
        "dependencies:\n"
        f"- repo_url: acme/evaluated\n  deployed_files: [{shared}]\n"
        f"- repo_url: acme/kept\n  deployed_files: [{shared}]\n"
        "deployments:\n"
        "- kind: project-relative\n  target: claude\n"
        f"  value: {shared}\n  runtime: null\n  scope: project\n"
        "  owners: [acme/evaluated, acme/kept]\n  active_owner: acme/kept\n"
        "  content_hash: null\n"
    )
    current = LockFile()
    for key in ("acme/evaluated", "acme/kept"):
        current.dependencies[key] = LockedDependency(repo_url=key)
    ctx = SimpleNamespace(
        package_deployed_files={"acme/evaluated": [shared]},
        existing_lockfile=existing,
        targets=[SimpleNamespace(name="claude", root_dir=".claude", primitives={})],
        project_root=tmp_path,
        native_carried_keys={"acme/kept"},
    )
    LockfileBuilder(ctx)._attach_deployed_files(current)
    (record,) = [
        record
        for record in DeploymentLedgerCodec.from_lockfile(current).records.values()
        if record.locator.value == shared
    ]
    assert record.active_owner == "acme/evaluated"
