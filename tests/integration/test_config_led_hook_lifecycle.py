"""Installed APM proof, not native-client execution, of hook migration safety."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path, PurePosixPath

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

pytestmark = [pytest.mark.e2e, pytest.mark.requires_apm_binary]

_TARGET_CONFIG = {
    "claude": ".claude/settings.json",
    "copilot": ".github/hooks/legacy-checks.json",
    "cursor": ".cursor/hooks.json",
    "codex": ".codex/hooks.json",
    "gemini": ".gemini/settings.json",
    "antigravity": ".agents/hooks.json",
    "windsurf": ".windsurf/hooks.json",
    "kiro": ".kiro/hooks",
}


def _hook(event: str = "PreToolUse", command: str = "echo harmless") -> dict:
    return {
        "hooks": {
            event: [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": command}]},
                {"matcher": "*", "hooks": [{"type": "command", "command": command}]},
                {"matcher": "Bash", "hooks": [{"type": "command", "command": command}]},
            ]
        }
    }


@pytest.mark.parametrize(
    "target,scope",
    [
        ("claude", "project"),
        ("copilot", "project"),
        ("claude", "user"),
        ("copilot", "user"),
        ("claude", "user-custom"),
    ],
)
def test_config_led_native_install_replay_update_and_removal(
    tmp_path: Path,
    apm_binary_path: Path,
    target: str,
    scope: str,
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / target, base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    sources = LocalPackageFactory(isolated.package_root)
    source = sources.create("checks", targets=(target,))
    descriptor = sources.add_hook(source, "checks", _hook(command="./checks/check.sh"))
    resource = sources.add_hook_asset(
        source, "checks", PurePosixPath("check.sh"), "#!/bin/sh\necho harmless\n"
    )
    consumers = LocalPackageFactory(isolated.work_root)
    consumer = consumers.create(
        "consumer",
        dependencies=({"path": str(source.root), "targets": [target]},),
        targets=("claude", "copilot"),
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    user_scope = scope != "project"
    if user_scope:
        shutil.copyfile(consumer.root / "apm.yml", isolated.config_root / "apm.yml")
    deploy_root = isolated.home if user_scope else consumer.root
    target_root = deploy_root / (
        ".claude" if target == "claude" else ".copilot" if user_scope else ".github"
    )
    if scope == "user-custom":
        target_root = isolated.home / "configured-claude"
        environment["CLAUDE_CONFIG_DIR"] = str(target_root)
        unused_default = isolated.home / ".claude/settings.json"
        unused_default.parent.mkdir(parents=True)
        unused_default.write_text("{unrelated malformed personal configuration")
    native = target_root / ("settings.json" if target == "claude" else "hooks/checks-checks.json")
    personal = target_root / ("settings.json" if target == "claude" else "hooks/personal.json")
    deployed_resource = target_root / (
        "hooks/checks/checks/check.sh"
        if target == "claude"
        else "hooks/scripts/checks/checks/check.sh"
    )
    personal.parent.mkdir(parents=True, exist_ok=True)
    personal.write_text(
        json.dumps(
            {
                "theme": "personal",
                "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo personal"}]}]},
            }
        )
    )

    def run(*args: str, succeeds: bool = True):
        if user_scope and args[0] != "audit":
            args = (args[0], "--global", *args[1:])
        result = runner.run(
            args,
            scenario_id=f"config-led-{target}-{'-'.join(args[:2])}",
            cwd=isolated.config_root if user_scope and args[0] == "audit" else consumer.root,
            env=environment,
        )
        assert (result.returncode == 0) == succeeds, result.stdout + result.stderr
        return result

    install = ("install", "--no-policy", "--parallel-downloads", "0")
    run(*install)
    first = native.read_bytes()
    entries = json.loads(first)["hooks"]["PreToolUse"]
    assert len(entries) == 2
    assert {entry["matcher"] for entry in entries} == {"Bash", "*"}
    other = deploy_root / (
        ".claude/apm-hooks.json"
        if target == "copilot"
        else ".copilot/hooks/checks-checks.json"
        if user_scope
        else ".github/hooks/checks-checks.json"
    )
    assert not other.exists()
    assert deployed_resource.read_bytes() == resource.read_bytes()
    original_resource = deployed_resource.read_bytes()
    if target == "claude":
        sidecar = json.loads((target_root / "apm-hooks.json").read_text())
        assert len(sidecar["PreToolUse"]) == 2
        assert len({entry["_apm_source"] for entry in sidecar["PreToolUse"]}) == 1
    run(*install)
    assert native.read_bytes() == first
    run("audit", "--no-policy")

    descriptor.write_text(json.dumps(_hook("InventedEvent")))
    resource.write_text("#!/bin/sh\necho updated resource\n")
    run(*install, succeeds=False)
    assert native.read_bytes() == first
    assert deployed_resource.read_bytes() == original_resource
    assert "personal" in personal.read_text()

    descriptor.write_text(json.dumps(_hook(command="./checks/check.sh --updated")))
    run(*install)
    assert "--updated" in native.read_text()
    assert deployed_resource.read_bytes() == resource.read_bytes()
    sources.set_targets(source, ("kiro",))
    run(*install)
    assert not deployed_resource.exists()
    if target == "claude":
        assert set(json.loads(native.read_text())["hooks"]) == {"Stop"}
    else:
        assert not native.exists()
    sources.set_targets(source, (target,))
    run(*install)
    assert deployed_resource.read_bytes() == resource.read_bytes()
    run("uninstall", str(source.root))
    if target == "claude":
        remaining = json.loads(personal.read_text())
        assert set(remaining["hooks"]) == {"Stop"}
        assert remaining["theme"] == "personal"
        assert not (target_root / "apm-hooks.json").exists()
    else:
        assert not native.exists()
        assert "echo personal" in personal.read_text()
    assert not deployed_resource.exists()
    if scope == "user-custom":
        assert unused_default.read_text() == "{unrelated malformed personal configuration"


def test_native_plugin_identity_is_not_disassembled_for_hook_migration(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    from apm_cli.copilot_plugins.registrar import catalog_path_for
    from tests.unit.copilot_plugins._builders import write_agent_plugin

    isolated = IsolatedApmEnvironment.create(tmp_path / "opaque", base_env=dict(os.environ))
    source = isolated.package_root / "opaque"
    write_agent_plugin(source, name="opaque", with_mcp=False)
    (source / "hooks").mkdir()
    descriptor = source / "hooks/claude-hooks.json"
    descriptor.write_text(json.dumps(_hook()))
    consumer = LocalPackageFactory(isolated.work_root).create(
        "consumer",
        dependencies=({"path": str(source)},),
        targets=("copilot",),
    )
    result = ApmLifecycleRunner((str(apm_binary_path),)).run(
        ("install", "--no-policy"),
        scenario_id="opaque-native-identity",
        cwd=consumer.root,
        env=isolated.subprocess_env(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    modules = consumer.root / "apm_modules"
    (plugin,) = json.loads(catalog_path_for(modules).read_text())["plugins"]
    native_source = modules / plugin["source"]
    assert (native_source / "hooks/claude-hooks.json").read_bytes() == descriptor.read_bytes()
    assert not (consumer.root / ".github/hooks").exists()


@pytest.mark.parametrize("target", list(_TARGET_CONFIG))
@pytest.mark.parametrize("shadowed", [True, False])
def test_explicit_targets_do_not_bypass_legacy_migration(
    tmp_path: Path, apm_binary_path: Path, target: str, shadowed: bool
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / target, base_env=dict(os.environ))
    sources = LocalPackageFactory(isolated.package_root)
    source = sources.create("legacy", targets=(target,))
    sources.add_hook(source, "checks", _hook())
    historical = target if shadowed else ("claude" if target == "copilot" else "copilot")
    sources.add_hook(source, f"{historical}-hooks", _hook())
    consumer = LocalPackageFactory(isolated.work_root).create(
        "consumer",
        dependencies=({"path": str(source.root), "targets": [target]},),
        targets=(target,),
    )
    result = ApmLifecycleRunner((str(apm_binary_path),)).run(
        ("install", "--no-policy"),
        scenario_id="legacy-migration",
        cwd=consumer.root,
        env=isolated.subprocess_env(),
    )
    assert result.returncode != 0, result.stdout + result.stderr
    output = " ".join((result.stdout + result.stderr).split())
    assert "previously excluded or shadowed" in output
    assert "targets declaration alone" in output
    assert not (consumer.root / ".claude/settings.json").exists()
    assert not (consumer.root / ".github/hooks").exists()
    assert not (consumer.root / _TARGET_CONFIG[target]).exists()


@pytest.mark.parametrize("target", list(_TARGET_CONFIG))
def test_withheld_hook_consent_does_not_apply_migration_or_activate_hooks(
    tmp_path: Path,
    apm_binary_path: Path,
    target: str,
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / target, base_env=dict(os.environ))
    sources = LocalPackageFactory(isolated.package_root)
    source = sources.create("legacy", targets=(target,))
    sources.add_hook(source, "checks", _hook())
    sources.add_hook(source, f"{target}-hooks", _hook())
    consumer = LocalPackageFactory(isolated.work_root).create(
        "consumer",
        dependencies=({"path": str(source.root)},),
        targets=(target,),
    )
    manifest_path = consumer.root / "apm.yml"
    manifest = load_yaml(manifest_path)
    manifest["allowExecutables"] = {}
    dump_yaml(manifest, manifest_path)
    result = ApmLifecycleRunner((str(apm_binary_path),)).run(
        ("install", "--no-policy"),
        scenario_id="hook-consent-withheld",
        cwd=consumer.root,
        env=isolated.subprocess_env(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "previously excluded or shadowed" not in " ".join(result.stdout.split())
    assert not (consumer.root / _TARGET_CONFIG[target]).exists()
    assert (consumer.root / "apm_modules/_local/legacy/apm.yml").is_file()
