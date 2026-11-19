"""Installed APM lifecycle for settings-only native Claude plugins (#3191, #3181).

Hooks run with a substituted host (CLAUDE_PLUGIN_ROOT set as Claude sets it);
this is not a native Claude execution witness.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.claude_plugins.registrar import marketplace_name
from apm_cli.utils.yaml_io import load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = [pytest.mark.integration, pytest.mark.e2e]

_RUNTIME_ROOT_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/native_claude_hooks"
_HOOK_EVENTS = ("SessionStart", "SubagentStart", "UserPromptSubmit")


@dataclass(frozen=True)
class _Scenario:
    isolated: IsolatedApmEnvironment
    env: dict[str, str]
    trap: Path

    @property
    def settings(self) -> Path:
        return self.isolated.home / ".claude/settings.json"

    @property
    def plugins(self) -> Path:
        return self.isolated.home / ".agents/plugins"

    def declared(self) -> dict:
        if not self.settings.exists():
            return {}
        return json.loads(self.settings.read_text(encoding="utf-8"))


def _native_scenario(tmp_path: Path) -> _Scenario:
    """Isolate HOME; a trap ``claude`` on PATH proves APM never runs it."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    env["CLAUDE_CONFIG_DIR"] = str(isolated.home / ".claude")
    trap = isolated.root / "claude-was-run"
    executable_dir = isolated.root / "executables"
    executable_dir.mkdir()
    for name in ("claude", "claude.cmd"):
        executable = executable_dir / name
        executable.write_text(f"#!/bin/sh\ntouch '{trap}'\nexit 91\n", encoding="ascii")
        executable.chmod(0o755)
    env["PATH"] = str(executable_dir) + os.pathsep + env["PATH"]
    return _Scenario(isolated, env, trap)


def test_runtime_plugin_root_hooks_stay_intact_per_plugin(
    tmp_path: Path, apm_engine_command: tuple[str, ...]
) -> None:
    """#3191: hooks that join process.env.CLAUDE_PLUGIN_ROOT keep their bundle.

    Two plugins deploy intact under distinct roots, are declared and enabled
    only through Claude settings, keep the user's explicit choices, and are
    removed owner-scoped.
    """
    scenario = _native_scenario(tmp_path)
    isolated, env = scenario.isolated, scenario.env
    factory = LocalGitRepositoryFactory(isolated.repository_root, env=env)
    repository = factory.create("runtime-root-hooks")
    for label in ("alpha", "beta"):
        source = repository.worktree / "plugins" / label
        shutil.copytree(_RUNTIME_ROOT_FIXTURE, source)
        manifest_path = source / ".claude-plugin/plugin.json"
        plugin_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        plugin_manifest["name"] = f"apm-hook-context-{label}"
        manifest_path.write_text(json.dumps(plugin_manifest), encoding="utf-8")
        (source / "resources/identity.txt").write_text(f"IDENTITY_{label}\n", encoding="ascii")
    revision = factory.commit(repository, message="Runtime plugin-root hook fixtures").sha
    dependency = "fixture/runtime-root-hooks"
    env = factory.url_rewrite_subprocess_env(repository, f"https://github.com/{dependency}.git")
    manifest = {
        "name": "hook-consumer",
        "version": "1.0.0",
        "targets": ["claude"],
        "dependencies": {
            "apm": [
                {"git": dependency, "ref": revision, "path": f"plugins/{label}"}
                for label in ("alpha", "beta")
            ]
        },
        # Pinned approval (#3902): the gate is on and nothing is approved yet.
        "executables": {"allow": {}},
    }
    (isolated.config_root / "apm.yml").write_text(json.dumps(manifest), encoding="utf-8")
    runner = ApmLifecycleRunner(apm_engine_command)

    def run(*args: str) -> None:
        result = runner.run(
            args, scenario_id="claude-native-runtime-root-hooks", cwd=isolated.work_root, env=env
        )
        assert result.returncode == 0, result.stdout + result.stderr

    names = {label: f"apm-hook-context-{label}" for label in ("alpha", "beta")}
    mkt = marketplace_name(isolated.config_root)
    keys = {label: f"{name}@{mkt}" for label, name in names.items()}
    mine = f"mine@{mkt}"
    # A user's own settings and a same-namespace entry APM did not write survive.
    scenario.settings.parent.mkdir(parents=True, exist_ok=True)
    scenario.settings.write_text(
        json.dumps({"theme": "dark", "enabledPlugins": {mine: True}}), encoding="utf-8"
    )

    # Unapproved: parked, neither deployed nor declared, and install succeeds.
    run("install", "--global", "--target", "claude", "--only", "apm")
    assert scenario.declared()["enabledPlugins"] == {mine: True}
    assert not scenario.plugins.exists()
    approve = runner.run(
        ("approve", "--all"), scenario_id="claude-native-approve", cwd=isolated.config_root, env=env
    )
    assert approve.returncode == 0, approve.stdout + approve.stderr
    pins = load_yaml(isolated.config_root / "apm.yml")["executables"]["allow"]
    assert len(pins) == 2 and all(grant["plugin"].startswith("sha256:") for grant in pins.values())
    run("install", "--global", "--target", "claude", "--only", "apm")
    declared = scenario.declared()
    assert declared["theme"] == "dark"
    assert declared["extraKnownMarketplaces"] == {
        mkt: {"source": {"source": "directory", "path": str(scenario.plugins)}}
    }
    assert declared["enabledPlugins"] == {mine: True, **dict.fromkeys(keys.values(), True)}
    catalog = json.loads((scenario.plugins / ".claude-plugin/marketplace.json").read_text())
    assert catalog["name"] == mkt
    assert sorted(entry["source"] for entry in catalog["plugins"]) == [
        f"./{names['alpha']}",
        f"./{names['beta']}",
    ]
    roots = {label: scenario.plugins / name for label, name in names.items()}
    for label, root in roots.items():
        assert (root / "hooks/hooks.json").read_bytes() == (
            _RUNTIME_ROOT_FIXTURE / "hooks/hooks.json"
        ).read_bytes()
        assert (root / "hooks/context.js").is_file()
        assert (root / "resources/identity.txt").read_text() == f"IDENTITY_{label}\n"
    lock = load_yaml(isolated.config_root / "apm.lock.yaml")
    records = sorted(dep["native_plugin"]["root"] for dep in lock["dependencies"])
    assert records == [f".agents/plugins/{names['alpha']}", f".agents/plugins/{names['beta']}"]
    claude_home = isolated.home / ".claude"
    assert not (claude_home / "apm-hooks.json").exists()
    assert not (claude_home / "hooks").exists()
    assert "hooks" not in declared

    # The user's explicit disable survives reinstall and a one-shot --target narrowing.
    declared["enabledPlugins"][keys["beta"]] = False
    scenario.settings.write_text(json.dumps(declared), encoding="utf-8")
    run("install", "--global", "--target", "claude", "--only", "apm")
    run("install", "--global", "--target", "codex", "--only", "apm")
    assert scenario.declared()["enabledPlugins"][keys["beta"]] is False
    assert all(root.is_dir() for root in roots.values())

    executed = shutil.which("node") is not None and os.name != "nt"
    if executed:
        witness = isolated.root / "hook-witness.jsonl"
        for root in roots.values():
            hook_env = dict(env, CLAUDE_PLUGIN_ROOT=str(root), APM_HOOK_PROBE_MARKER=str(witness))
            hooks = json.loads((root / "hooks/hooks.json").read_text(encoding="utf-8"))
            for event in _HOOK_EVENTS:
                completed = subprocess.run(
                    hooks["hooks"][event][0]["hooks"][0]["command"],
                    shell=True,
                    input=json.dumps({"hook_event_name": event}),
                    text=True,
                    capture_output=True,
                    env=hook_env,
                    cwd=isolated.work_root,
                    timeout=30,
                    check=False,
                )
                assert completed.returncode == 0, completed.stderr
        rows = [json.loads(line) for line in witness.read_text().splitlines() if line.strip()]
        assert sorted((Path(row["root"]).name, row["event"], row["resource"]) for row in rows) == [
            (names[label], event, f"IDENTITY_{label}")
            for label in ("alpha", "beta")
            for event in sorted(_HOOK_EVENTS)
        ]

    run("uninstall", "--global", f"{dependency}/plugins/alpha")
    assert not roots["alpha"].exists()
    assert roots["beta"].is_dir()
    after = scenario.declared()
    assert keys["alpha"] not in after["enabledPlugins"]
    assert after["enabledPlugins"][mine] is True
    run("uninstall", "--global", f"{dependency}/plugins/beta")
    final = scenario.declared()
    assert final["extraKnownMarketplaces"] == {}
    # APM never removes a key it did not produce, nor the user's explicit false.
    assert final["enabledPlugins"] == {mine: True, keys["beta"]: False}
    assert final["theme"] == "dark"
    assert not scenario.plugins.exists()
    assert not scenario.trap.exists()
    if not executed:
        pytest.skip("Declaration checks passed; hook execution needs node on POSIX.")


@pytest.mark.parametrize("failure", ["", "install", "uninstall"])
@pytest.mark.parametrize("local", [False, True])
def test_installed_hook_source_reinstall_and_removal(
    tmp_path: Path, apm_engine_command: tuple[str, ...], failure: str, local: bool
) -> None:
    """Run acquisition, consent, lock, scan, audit and removal through the installed CLI."""
    scenario = _native_scenario(tmp_path)
    isolated, env = scenario.isolated, scenario.env
    factory = LocalGitRepositoryFactory(isolated.repository_root, env=env)
    repository = factory.create("hooks")
    source = repository.worktree / "plugins/context"
    (source / ".claude-plugin").mkdir(parents=True)
    (source / ".claude-plugin/plugin.json").write_text(
        '{"name":"owned-hooks","version":"1.0.0"}', encoding="ascii"
    )
    (source / "hooks").mkdir()
    hooks = {
        "hooks": {
            event: [{"hooks": [{"type": "command", "command": "echo NEVER_EXECUTE"}]}]
            for event in _HOOK_EVENTS
        }
    }
    (source / "hooks/hooks.json").write_text(json.dumps(hooks), encoding="utf-8")
    native_skill = source / "skills/review/SKILL.md"
    native_skill.parent.mkdir(parents=True)
    native_skill.write_text(
        "---\nname: review\ndescription: Native review\n---\nOwned native skill.\n",
        encoding="ascii",
    )
    personal_skill = isolated.home / ".claude/skills/review/SKILL.md"
    personal_skill.parent.mkdir(parents=True)
    personal_skill.write_bytes(b"Personal loose skill must not block native registration.\n")
    personal_before = personal_skill.read_bytes()
    (source / "__pycache__").mkdir()
    (source / "__pycache__/resource.bin").write_bytes(b"publisher-owned resource")
    (source / "hooks/resource.cjs").write_text("// inert publisher resource\n", encoding="ascii")
    revision = factory.commit(repository, message="Owned inert hook fixture").sha
    dependency = "fixture/native-hooks"
    package_argument = str(source) if local else dependency + "/plugins/context"
    env = factory.url_rewrite_subprocess_env(repository, f"https://github.com/{dependency}.git")
    manifest = {
        "name": "hook-consumer",
        "version": "1.0.0",
        "targets": ["claude"],
        "dependencies": {
            "apm": [
                str(source)
                if local
                else {"git": dependency, "ref": revision, "path": "plugins/context"}
            ]
        },
        "executables": {"allow": {}},
    }
    (isolated.config_root / "apm.yml").write_text(json.dumps(manifest), encoding="utf-8")
    runner = ApmLifecycleRunner(apm_engine_command)
    deployed = scenario.plugins / "owned-hooks"

    def run(*args: str, expected: int = 0) -> str:
        result = runner.run(
            args,
            scenario_id="claude-native-hook-lifecycle",
            cwd=isolated.config_root if args[0] == "audit" else isolated.work_root,
            env=env,
        )
        assert result.returncode == expected, result.stdout + result.stderr
        if args[0] == "audit":
            return result.stdout
        return " ".join((result.stdout + result.stderr).split())

    def corrupt_settings() -> bytes:
        scenario.settings.parent.mkdir(parents=True, exist_ok=True)
        # Valid JSON other readers accept; only the native declaration refuses it.
        scenario.settings.write_text('{"enabledPlugins": []}', encoding="utf-8")
        return scenario.settings.read_bytes()

    run("install", "--global", "--target", "claude", "--only", "apm")
    assert not deployed.exists()
    approved = runner.run(
        ("approve", "--all"), scenario_id="claude-native-approve", cwd=isolated.config_root, env=env
    )
    assert approved.returncode == 0, approved.stdout + approved.stderr
    if failure == "install":
        (isolated.config_root / "apm.lock.yaml").unlink(missing_ok=True)
        before = corrupt_settings()
        output = run("install", "--global", "--target", "claude", "--only", "apm", expected=1)
        assert "Claude native plugin declaration failed" in output
        assert "Failed to resolve APM dependencies" not in output
        assert scenario.settings.read_bytes() == before
        # One rollback unit: a first install that cannot declare keeps no lock or roots.
        assert not (isolated.config_root / "apm.lock.yaml").exists()
        assert not scenario.plugins.exists()
        scenario.settings.unlink()

    for flags in ((), (), ("--frozen",)):
        run("install", "--global", "--target", "claude", "--only", "apm", *flags)
        assert list(scenario.declared()["enabledPlugins"].values()) == [True]
        assert next(iter(scenario.declared()["enabledPlugins"])) == (
            f"owned-hooks@{marketplace_name(isolated.config_root)}"
        )
        assert json.loads((deployed / "hooks/hooks.json").read_text()) == hooks
        assert (deployed / "skills/review/SKILL.md").read_bytes() == native_skill.read_bytes()
        assert (deployed / "__pycache__/resource.bin").is_file()
        assert personal_skill.read_bytes() == personal_before
        lock = load_yaml(isolated.config_root / "apm.lock.yaml")
        entry = lock["dependencies"][0]
        assert entry["native_plugin"]["root"] == ".agents/plugins/owned-hooks"
        if local:
            assert entry["local_path"] == str(source)
        else:
            assert entry["resolved_commit"] == revision
        assert not (isolated.home / ".claude/hooks").exists()

    if not failure:
        audit = ("audit", "--ci", "--no-policy", "--no-fail-fast", "--format", "json")
        healthy = json.loads(run(*audit))
        assert [row["name"] for row in healthy["checks"] if not row["passed"]] == []
        settings_before = scenario.settings.read_bytes()
        asset = deployed / "__pycache__/resource.bin"
        original = asset.read_bytes()
        asset.write_bytes(b"tampered deployed resource")
        output = run(*audit, expected=1)
        assert "differs from apm.lock.yaml" in output
        assert asset.read_bytes() == b"tampered deployed resource"
        asset.write_bytes(original)
        if local:
            original_asset = source / "__pycache__/resource.bin"
            original_asset.write_bytes(b"changed local source")
            assert "differs from apm.lock.yaml" in run(*audit, expected=1)
            original_asset.write_bytes(original)
        if os.name != "nt":
            script = deployed / "hooks/resource.cjs"
            mode = script.stat().st_mode
            script.chmod(mode ^ stat.S_IXUSR)
            assert "differs from apm.lock.yaml" in run(*audit, expected=1)
            script.chmod(mode)
        restored = json.loads(run(*audit))
        assert next(row for row in restored["checks"] if row["name"] == "drift")["passed"] is True
        assert scenario.settings.read_bytes() == settings_before

    fails_removal = failure == "uninstall"
    if fails_removal:
        corrupt_settings()
    output = run("uninstall", "--global", package_argument, expected=1 if fails_removal else 0)
    lock_path = isolated.config_root / "apm.lock.yaml"
    assert lock_path.exists() is fails_removal
    assert load_yaml(isolated.config_root / "apm.yml")["dependencies"]["apm"] == []
    if fails_removal:
        # The lock is restored, so the retry still owns what it must retire.
        restored = load_yaml(lock_path)["dependencies"]
        assert [dep["native_plugin"]["name"] for dep in restored] == ["owned-hooks"]
        assert "Package removal may already be complete" in output
        assert "do not re-add the removed package" in output
        scenario.settings.unlink()
        run("install", "--global", "--target", "claude", "--only", "apm")
    else:
        assert "native Claude plugin(s) removed" in output
    assert scenario.declared().get("enabledPlugins", {}) == {}
    assert not deployed.exists()
    assert personal_skill.read_bytes() == personal_before
    assert not scenario.trap.exists()


def test_native_plugin_mcp_server_is_loaded_by_claude_not_written_by_apm(
    tmp_path: Path, apm_engine_command: tuple[str, ...]
) -> None:
    """#3290: Claude loads a native plugin's .mcp.json; APM writes it only for other targets."""
    scenario = _native_scenario(tmp_path)
    isolated, env = scenario.isolated, scenario.env
    factory = LocalGitRepositoryFactory(isolated.repository_root, env=env)
    repository = factory.create("mcp-plugin")
    source = repository.worktree / "plugins/tracker"
    (source / ".claude-plugin").mkdir(parents=True)
    (source / ".claude-plugin/plugin.json").write_text(
        '{"name":"owned-mcp","version":"1.0.0"}', encoding="ascii"
    )
    server = {"command": "node", "args": ["${CLAUDE_PLUGIN_ROOT}/server.js"]}
    (source / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"plugin-tracker": server}}), encoding="utf-8"
    )
    (source / "server.js").write_text("// inert\n", encoding="ascii")
    revision = factory.commit(repository, message="Native MCP plugin fixture").sha
    dependency = "fixture/native-mcp"
    env = factory.url_rewrite_subprocess_env(repository, f"https://github.com/{dependency}.git")
    manifest = {
        "name": "mcp-consumer",
        "version": "1.0.0",
        "dependencies": {"apm": [{"git": dependency, "ref": revision, "path": "plugins/tracker"}]},
    }
    (isolated.config_root / "apm.yml").write_text(json.dumps(manifest), encoding="utf-8")
    runner = ApmLifecycleRunner(apm_engine_command)

    def install(target: str) -> None:
        result = runner.run(
            ("install", "--global", "--target", target),
            scenario_id="claude-native-mcp",
            cwd=isolated.work_root,
            env=env,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def claude_mcp_names() -> set[str]:
        names: set[str] = set()
        for path in (isolated.home / ".claude.json", isolated.home / ".claude/.claude.json"):
            if path.exists():
                names |= set(json.loads(path.read_text(encoding="utf-8")).get("mcpServers", {}))
        return names

    install("claude")
    deployed = scenario.plugins / "owned-mcp"
    assert json.loads((deployed / ".mcp.json").read_text())["mcpServers"] == {
        "plugin-tracker": server
    }
    assert "plugin-tracker" not in claude_mcp_names()
    lock = load_yaml(isolated.config_root / "apm.lock.yaml")
    assert lock["dependencies"][0]["native_plugin"]["name"] == "owned-mcp"
    assert "plugin-tracker" not in (lock.get("mcp_target_servers") or {}).get("claude", [])

    # Mixed targets: Copilot still gets APM's copy; Claude keeps only the plugin's.
    install("claude,copilot")
    copilot = json.loads((isolated.home / ".copilot/mcp-config.json").read_text())
    assert "plugin-tracker" in copilot["mcpServers"]
    assert "plugin-tracker" not in claude_mcp_names()
    assert (deployed / ".mcp.json").is_file()
    assert not scenario.trap.exists()
