"""Local plugin bundle installs register hooks like a source install (#3700).

``apm pack`` emits a plugin bundle whose hooks live in ``hooks.json`` plus
``hooks/`` resources. ``apm install <bundle>`` must route them through the
hook integrator a source install uses, behind the exact-content executables
gate, keep ``apm audit --ci`` green, and keep them when an unrelated
dependency is installed or uninstalled.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from apm_cli.bundle.local_bundle import detect_local_bundle
from apm_cli.security.executables import local_bundle_approval_key
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._marketplace_pack_kit import (
    MarketplacePackWorld,
    output,
    skill_md,
    write,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
]


def _hooked_package(root: Path, name: str, matcher: str, skill: str) -> Path:
    write(
        root / "apm.yml",
        f"name: {name}\nversion: 1.0.0\ndescription: h\nlicense: MIT\ntargets: [claude, copilot]\n"
        "dependencies:\n  apm: []\n",
    )
    write(root / ".apm" / "skills" / skill / "SKILL.md", skill_md(skill))
    hook = {
        "hooks": {
            "PreToolUse": [
                {"matcher": matcher, "hooks": [{"type": "command", "command": "./scripts/x.sh"}]}
            ]
        }
    }
    write(root / ".apm" / "hooks" / "hooks.json", json.dumps(hook))
    script = write(root / ".apm" / "hooks" / "scripts" / "x.sh", f"#!/bin/sh\necho {name}\n")
    script.chmod(0o755)
    (root / ".claude").mkdir()
    return root


def _allow(project: Path, *keys: str) -> None:
    manifest = load_yaml(project / "apm.yml")
    manifest["executables"] = {"allow": {key: {"hooks": True} for key in keys}}
    dump_yaml(manifest, project / "apm.yml")


def _packed_bundle(world: MarketplacePackWorld, tmp_path: Path) -> tuple[Path, Path, str]:
    producer = _hooked_package(world.isolated.work_root / "hookpkg", "hookpkg", "Bash", "hs")
    world.apm(producer, "install", "--no-policy")
    world.apm(producer, "pack", "-o", str(tmp_path / "out"))
    bundle = tmp_path / "out" / "hookpkg-1.0.0"
    info = detect_local_bundle(bundle)
    key = local_bundle_approval_key(
        info.package_id, str(info.plugin_json.get("version") or ""), info.source_dir, info.lockfile
    )
    return producer, bundle, key


def _hook_entries(project: Path) -> dict[str, list[str]]:
    settings = json.loads((project / ".claude" / "settings.json").read_text())
    return {
        event: [entry["matcher"] for entry in entries]
        for event, entries in settings["hooks"].items()
    }


def _run_hook(world: MarketplacePackWorld, project: Path, matcher: str) -> str:
    settings = json.loads((project / ".claude" / "settings.json").read_text())
    (command,) = [
        hook["command"]
        for entry in settings["hooks"]["PreToolUse"]
        if entry["matcher"] == matcher
        for hook in entry["hooks"]
    ]
    ran = subprocess.run(
        ["sh", "-c", command],
        cwd=project,
        env={**world.env, "CLAUDE_PROJECT_DIR": str(project)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert ran.returncode == 0, ran.stderr
    return ran.stdout


def test_bundle_hooks_park_then_register_like_a_source_install(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    producer, bundle, key = _packed_bundle(world, tmp_path)

    # Source install of the same package: the reference registration.
    source = world.consumer("source-consumer", targets=("claude",))
    world.apm(source, "install", str(producer), "--no-policy")
    world.apm(source, "approve", str(producer))
    world.apm(source, "install", "--no-policy")
    assert _hook_entries(source) == {"PreToolUse": ["Bash"]}

    # executables: {} parks bundle hooks, naming the exact approval key.
    consumer = world.consumer("bundle-consumer", targets=("claude",))
    manifest = load_yaml(consumer / "apm.yml")
    manifest["executables"] = {}
    dump_yaml(manifest, consumer / "apm.yml")
    text = output(world.apm(consumer, "install", str(bundle), "--no-policy"))
    assert "Skipped 2 bundle hook file(s) from hookpkg" in text, text
    assert "local bundle install does not register plugin hooks" not in text
    assert not (consumer / ".claude" / "settings.json").exists()
    assert not (consumer / ".claude" / "hooks").exists()
    assert (consumer / ".claude" / "skills" / "hs" / "SKILL.md").is_file()

    # Approved: a dry run still writes no hook config or scripts.
    _allow(consumer, key)
    world.apm(consumer, "install", str(bundle), "--no-policy", "--dry-run")
    assert not (consumer / ".claude" / "settings.json").exists()
    assert not (consumer / ".claude" / "hooks").exists()

    # Approved: registered through the same merge path as the source install.
    world.apm(consumer, "install", str(bundle), "--no-policy")
    assert _hook_entries(consumer) == _hook_entries(source)
    assert _run_hook(world, consumer, "Bash") == _run_hook(world, source, "Bash") == "hookpkg\n"
    sidecar = json.loads((consumer / ".claude" / "apm-hooks.json").read_text())
    assert [e["_apm_source"] for e in sidecar["PreToolUse"]] == ["local-bundle::hookpkg"]
    assert list(consumer.rglob("*.sh.json")) == []
    lock = world.lock(consumer)
    scripts = [p for p in lock["local_deployed_files"] if p.endswith("/x.sh")]
    assert scripts == [".claude/hooks/hookpkg/hooks/scripts/x.sh"], lock
    world.apm(consumer, "audit", "--ci", "--no-policy")

    # Reinstalling the same bundle replaces its entries instead of duplicating them.
    world.apm(consumer, "install", str(bundle), "--no-policy")
    assert _hook_entries(consumer) == {"PreToolUse": ["Bash"]}

    # An unrelated dependency with hooks neither sweeps nor drifts bundle hooks.
    dep = _hooked_package(world.isolated.work_root / "otherpkg", "otherpkg", "Write", "os")
    _allow(consumer, key, f"{dep}#1.0.0")
    world.apm(consumer, "install", str(dep), "--no-policy")
    assert _hook_entries(consumer) == {"PreToolUse": ["Bash", "Write"]}
    world.apm(consumer, "audit", "--ci", "--no-policy")
    world.apm(consumer, "uninstall", str(dep))
    assert _hook_entries(consumer) == {"PreToolUse": ["Bash"]}
    assert _run_hook(world, consumer, "Bash") == "hookpkg\n"
    assert (consumer / ".claude" / "skills" / "hs" / "SKILL.md").is_file()
    world.apm(consumer, "audit", "--ci", "--no-policy")


def test_bundle_hooks_register_for_copilot_and_user_scope(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    producer, bundle, key = _packed_bundle(world, tmp_path)

    # Copilot keeps one hook file per package; the bundle's matches a source install's.
    source = world.consumer("copilot-source", targets=("copilot",))
    world.apm(source, "install", str(producer), "--no-policy")
    world.apm(source, "approve", str(producer))
    world.apm(source, "install", "--no-policy")
    consumer = world.consumer("copilot-bundle", targets=("copilot",))
    _allow(consumer, key)
    world.apm(consumer, "install", str(bundle), "--no-policy")
    world.apm(consumer, "install", str(bundle), "--no-policy")

    def copilot_hooks(project: Path) -> dict[str, list[str]]:
        (hook_file,) = (project / ".github" / "hooks").glob("*.json")
        data = json.loads(hook_file.read_text())
        return {event: [e["matcher"] for e in entries] for event, entries in data["hooks"].items()}

    assert copilot_hooks(consumer) == copilot_hooks(source) == {"preToolUse": ["Bash"]}
    world.apm(consumer, "audit", "--ci", "--no-policy")

    # User scope registers once, with a cwd-independent command.
    home = world.isolated.home
    (home / ".claude").mkdir(exist_ok=True)
    write(home / ".apm" / "apm.yml", "name: user\nversion: 0.1.0\ntargets: [claude]\n")
    _allow(home / ".apm", key)
    world.apm(world.isolated.work_root, "install", "-g", str(bundle), "--no-policy")
    world.apm(world.isolated.work_root, "install", "-g", str(bundle), "--no-policy")
    assert _hook_entries(home) == {"PreToolUse": ["Bash"]}
    assert _run_hook(world, home, "Bash") == "hookpkg\n"
