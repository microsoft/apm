"""Installed APM lifecycle with metadata transport substituted, not native execution."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.claude_native_protocol import NATIVE_CLIENT_STUB
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = [pytest.mark.integration, pytest.mark.e2e]

_FAILURE_STUB = r"""
from apm_cli.claude_plugins.admission import ClaudeNativePluginError
_original_native_run = ClaudePluginCLI.run
def _controlled_native_run(self, *args):
    if len(args) > 1 and os.environ.get("APM_NATIVE_FAIL") == args[1]:
        raise ClaudeNativePluginError("deliberate metadata transport failure")
    return _original_native_run(self, *args)
ClaudePluginCLI.run = _controlled_native_run
"""


_RUNTIME_ROOT_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/native_claude_hooks"
_HOOK_EVENTS = ("SessionStart", "SubagentStart", "UserPromptSubmit")


def _native_scenario(tmp_path: Path) -> tuple[IsolatedApmEnvironment, dict[str, str], Path]:
    """Isolate HOME and substitute only the external Claude metadata transport."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    env["CLAUDE_CONFIG_DIR"] = str(isolated.home / ".claude")
    state_file = isolated.root / "native-state.json"
    env["APM_NATIVE_TEST_STATE"] = str(state_file)
    executable_dir = isolated.root / "executables"
    executable_dir.mkdir()
    for name in ("claude", "claude.cmd"):
        executable = executable_dir / name
        executable.write_text("exit 91\n", encoding="ascii")
        executable.chmod(0o755)
    env["PATH"] = str(executable_dir) + os.pathsep + env["PATH"]
    guard = isolated.root / "network_guard/sitecustomize.py"
    guard.write_text(
        guard.read_text(encoding="utf-8") + NATIVE_CLIENT_STUB + _FAILURE_STUB, encoding="utf-8"
    )
    return isolated, env, state_file


def test_runtime_plugin_root_hooks_stay_intact_per_plugin(
    tmp_path: Path, apm_engine_command: tuple[str, ...]
) -> None:
    """#3191: hooks that join process.env.CLAUDE_PLUGIN_ROOT keep their bundle.

    The command has no ./ path, so loose projection registered it without its
    script. Two plugins must each register intact under a distinct root, with
    no loose Claude hook registration, and removal must be owner-scoped.
    """
    isolated, env, state_file = _native_scenario(tmp_path)
    factory = LocalGitRepositoryFactory(isolated.repository_root, env=env)
    repository = factory.create("runtime-root-hooks")
    sources: dict[str, Path] = {}
    for label in ("alpha", "beta"):
        source = repository.worktree / "plugins" / label
        shutil.copytree(_RUNTIME_ROOT_FIXTURE, source)
        manifest_path = source / ".claude-plugin/plugin.json"
        plugin_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        plugin_manifest["name"] = f"apm-hook-context-{label}"
        manifest_path.write_text(json.dumps(plugin_manifest), encoding="utf-8")
        (source / "resources/identity.txt").write_text(f"IDENTITY_{label}\n", encoding="ascii")
        sources[label] = source
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
                for label in sources
            ]
        },
        "allowExecutables": {
            f"github.com/{dependency}/plugins/{label}#{revision}": {"hooks": True}
            for label in sources
        },
    }
    (isolated.config_root / "apm.yml").write_text(json.dumps(manifest), encoding="utf-8")
    runner = ApmLifecycleRunner(apm_engine_command)

    def run(*args: str) -> None:
        result = runner.run(
            args, scenario_id="claude-native-runtime-root-hooks", cwd=isolated.work_root, env=env
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def registered() -> dict[str, Path]:
        plugins = json.loads(state_file.read_text(encoding="utf-8"))["plugins"]
        return {Path(p["readFromFolder"]).name: Path(p["readFromFolder"]) for p in plugins}

    run("install", "--global", "--target", "claude", "--only", "apm")
    roots = registered()
    assert sorted(roots) == ["alpha", "beta"]
    assert roots["alpha"] != roots["beta"]
    plugins = json.loads(state_file.read_text(encoding="utf-8"))["plugins"]
    assert all(plugin["enabled"] is False for plugin in plugins)
    fixture_hooks = (_RUNTIME_ROOT_FIXTURE / "hooks/hooks.json").read_bytes()
    for label, root in roots.items():
        assert root.is_relative_to(isolated.config_root / "apm_modules")
        assert (root / "hooks/hooks.json").read_bytes() == fixture_hooks
        assert (root / "hooks/context.js").read_bytes() == (
            _RUNTIME_ROOT_FIXTURE / "hooks/context.js"
        ).read_bytes()
        assert (root / "resources/identity.txt").read_text() == f"IDENTITY_{label}\n"
    claude_home = isolated.home / ".claude"
    assert not (claude_home / "settings.json").exists()
    assert not (claude_home / "apm-hooks.json").exists()
    assert not (claude_home / "hooks").exists()

    # Substituted host: Claude sets CLAUDE_PLUGIN_ROOT per plugin; this is not
    # a native execution witness (see the PR's native hook check).
    node = shutil.which("node")
    if node is not None and os.name != "nt":
        witness = isolated.root / "hook-witness.jsonl"
        for root in roots.values():
            hook_env = dict(env, CLAUDE_PLUGIN_ROOT=str(root), APM_HOOK_PROBE_MARKER=str(witness))
            declared = json.loads((root / "hooks/hooks.json").read_text(encoding="utf-8"))
            for event in _HOOK_EVENTS:
                command = declared["hooks"][event][0]["hooks"][0]["command"]
                completed = subprocess.run(
                    command,
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
            (label, event, f"IDENTITY_{label}")
            for label in ("alpha", "beta")
            for event in sorted(_HOOK_EVENTS)
        ]

    run("uninstall", "--global", f"{dependency}/plugins/beta")
    assert sorted(registered()) == ["alpha"]
    assert not roots["beta"].exists()
    assert (roots["alpha"] / "hooks/context.js").is_file()
    run("uninstall", "--global", f"{dependency}/plugins/alpha")
    assert registered() == {}
    calls = json.loads(state_file.read_text(encoding="utf-8"))["calls"]
    assert all(call[1:2] not in (["enable"], ["disable"]) for call in calls)


@pytest.mark.parametrize("failure", ["", "install", "uninstall"])
@pytest.mark.parametrize("local", [False, True])
def test_installed_hook_source_reinstall_and_removal(
    tmp_path: Path, apm_engine_command: tuple[str, ...], failure: str, local: bool
) -> None:
    """Run acquisition, consent, lock, scan and removal through the installed CLI."""
    isolated, env, state_file = _native_scenario(tmp_path)
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
            for event in ("SessionStart", "SubagentStart", "UserPromptSubmit")
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
        "allowExecutables": {
            str(source) if local else f"github.com/{dependency}/plugins/context#{revision}": {
                "hooks": True
            }
        },
    }
    (isolated.config_root / "apm.yml").write_text(json.dumps(manifest), encoding="utf-8")
    runner = ApmLifecycleRunner(apm_engine_command)

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

    if failure == "install":
        env["APM_NATIVE_FAIL"] = "install"
        output = run("install", "--global", "--target", "claude", "--only", "apm", expected=1)
        assert "deliberate metadata transport failure" in output
        assert "Failed to resolve APM dependencies" not in output
        assert (isolated.config_root / "apm.lock.yaml").exists()
        env["APM_NATIVE_FAIL"] = ""

    for flags in ((), (), ("--frozen",)):
        run("install", "--global", "--target", "claude", "--only", "apm", *flags)
        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert len(state["plugins"]) == 1
        plugin = state["plugins"][0]
        assert plugin["enabled"] is False
        live_source = Path(plugin["readFromFolder"])
        assert live_source.is_relative_to(isolated.config_root / "apm_modules")
        assert json.loads((live_source / "hooks/hooks.json").read_text()) == hooks
        assert (live_source / "skills/review/SKILL.md").read_bytes() == native_skill.read_bytes()
        assert personal_skill.read_bytes() == personal_before
        lock = load_yaml(isolated.config_root / "apm.lock.yaml")
        if local:
            assert lock["dependencies"][0]["local_path"] == str(source)
        else:
            assert lock["dependencies"][0]["resolved_commit"] == revision
        assert not (isolated.home / ".claude/settings.json").exists()
        assert not (isolated.home / ".claude/hooks").exists()

    if not failure:
        audit = ("audit", "--ci", "--no-policy", "--no-fail-fast", "--format", "json")
        healthy = json.loads(run(*audit))
        assert [row["name"] for row in healthy["checks"] if not row["passed"]] == []
        assert next(row for row in healthy["checks"] if row["name"] == "drift")["passed"] is True
        ledger = isolated.config_root / "apm_modules/.claude-plugin/apm-registration.json"
        baseline = ledger.read_bytes()
        calls_before = state_file.read_bytes()
        asset = live_source / "__pycache__/resource.bin"
        original = asset.read_bytes()
        asset.write_bytes(b"tampered publisher resource")
        output = run(*audit, expected=1)
        assert "fingerprint changed" in output
        assert (
            next(row for row in json.loads(output)["checks"] if row["name"] == "drift")["passed"]
            is False
        )
        assert asset.read_bytes() == b"tampered publisher resource"
        asset.write_bytes(original)
        if local:
            original_asset = source / "__pycache__/resource.bin"
            original_asset.write_bytes(b"changed local source")
            assert "fingerprint changed" in run(*audit, expected=1)
            assert asset.read_bytes() == original
            assert original_asset.read_bytes() == b"changed local source"
            original_asset.write_bytes(original)
        if os.name != "nt":
            script = live_source / "hooks/resource.cjs"
            mode = script.stat().st_mode
            script.chmod(mode ^ stat.S_IXUSR)
            assert "fingerprint changed" in run(*audit, expected=1)
            assert script.stat().st_mode & stat.S_IXUSR != mode & stat.S_IXUSR
            script.chmod(mode)
        restored = json.loads(run(*audit))
        assert next(row for row in restored["checks"] if row["name"] == "drift")["passed"] is True
        assert ledger.read_bytes() == baseline
        assert state_file.read_bytes() == calls_before

    fails_removal = failure == "uninstall"
    env["APM_NATIVE_FAIL"] = "uninstall" if fails_removal else ""
    output = run("uninstall", "--global", package_argument, expected=1 if fails_removal else 0)
    assert not live_source.exists()
    lock_path = isolated.config_root / "apm.lock.yaml"
    assert lock_path.exists() is fails_removal
    assert load_yaml(isolated.config_root / "apm.yml")["dependencies"]["apm"] == []
    if fails_removal:
        assert load_yaml(lock_path)["dependencies"] == []
        assert "Package removal may already be complete" in output
        assert "original --target selection" in output
        assert "do not re-add the removed package" in output
        assert json.loads(state_file.read_text())["plugins"]
        env["APM_NATIVE_FAIL"] = ""
        run("install", "--global", "--target", "claude", "--only", "apm")
        assert json.loads(state_file.read_text())["plugins"] == []
    else:
        assert "native Claude plugin(s) removed" in output
        assert json.loads(state_file.read_text())["plugins"] == []
    state = json.loads(state_file.read_text())
    assert [call[1] for call in state["calls"] if call[1:2] in (["install"], ["update"])] == [
        "install",
        "update",
        "update",
    ]
    assert all(call[1:2] not in (["enable"], ["disable"]) for call in state["calls"])
    assert personal_skill.read_bytes() == personal_before
