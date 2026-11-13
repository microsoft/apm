"""Lifecycle guards for marketplace/pack producer surfaces (#3670).

1. A marketplace package on a branch ref fails ``apm pack`` with a recovery
   command that exists (no phantom ``--allow-head`` flag).
2. ``apm marketplace package add/set --ref`` refuses a tag the reachable
   remote does not have, exactly as ``apm marketplace check`` would, and
   leaves ``apm.yml`` byte-identical; an existing tag is stored.
3. ``apm pack`` carries a hook script referenced as ``./scripts/x.sh`` from
   ``.apm/hooks/``, the way ``apm install`` resolves it, and points the packed
   command at the packed file.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

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

_SOURCE = "git.example.invalid/acme/catpkg"


def _producer(world: MarketplacePackWorld, packages: list[dict]) -> Path:
    root = world.isolated.work_root / "producer"
    root.mkdir()
    dump_yaml(
        {
            "name": "producer",
            "version": "1.0.0",
            "description": "p",
            "marketplace": {
                "owner": {"name": "acme", "url": "https://example.invalid/acme"},
                "build": {"tagPattern": "v{version}"},
                "packages": packages,
            },
        },
        root / "apm.yml",
    )
    return root


def _published(world: MarketplacePackWorld) -> str:
    pkg = world.isolated.package_root / "catpkg"
    write(pkg / "apm.yml", "name: catpkg\nversion: 1.0.0\ndescription: c\n")
    write(pkg / "skills" / "c1" / "SKILL.md", skill_md("c1"))
    repository, commit = world.publish("catpkg", pkg)
    world.repos.tag(repository, "v1.0.0", commit)
    return commit.sha


def test_branch_ref_pack_failure_names_a_real_recovery(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _published(world)
    producer = _producer(
        world, [{"name": "catpkg", "description": "c", "source": _SOURCE, "ref": "main"}]
    )
    result = world.apm(producer, "pack", expect=1)
    text = output(result)
    assert "branch/HEAD ref 'main'" in text, text
    assert "--allow-head" not in text, text
    assert "apm marketplace package set catpkg --ref" in text, text
    assert not (producer / ".claude-plugin" / "marketplace.json").exists()

    # The suggested recovery works: set resolves to a pin and pack succeeds.
    world.apm(producer, "marketplace", "package", "set", "catpkg", "--ref", "v1.0.0")
    assert load_yaml(producer / "apm.yml")["marketplace"]["packages"][0]["ref"] == "v1.0.0"
    world.apm(producer, "pack")


def test_package_add_and_set_refuse_a_missing_tag(tmp_path: Path, apm_binary_path: Path) -> None:
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    sha = _published(world)
    producer = _producer(world, [])
    before = (producer / "apm.yml").read_bytes()

    missing = world.apm(
        producer, "marketplace", "package", "add", _SOURCE, "--ref", "v9.9.9", expect=2
    )
    text = output(missing)
    assert "Ref 'v9.9.9' not found" in text, text
    assert "network unavailable" not in text, text
    assert (producer / "apm.yml").read_bytes() == before

    world.apm(producer, "marketplace", "package", "add", _SOURCE, "--ref", "v1.0.0")
    entry = load_yaml(producer / "apm.yml")["marketplace"]["packages"][0]
    assert entry["ref"] == "v1.0.0"
    after_add = (producer / "apm.yml").read_bytes()

    world.apm(producer, "marketplace", "package", "set", "catpkg", "--ref", "nope", expect=2)
    assert (producer / "apm.yml").read_bytes() == after_add

    # A branch still resolves to its commit, and check agrees with what add stored.
    world.apm(producer, "marketplace", "package", "set", "catpkg", "--ref", "main")
    assert load_yaml(producer / "apm.yml")["marketplace"]["packages"][0]["ref"] == sha
    world.apm(producer, "marketplace", "check")


def test_pack_carries_relative_hook_script_like_install(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    root = world.isolated.work_root / "hooked"
    write(
        root / "apm.yml",
        "name: hooked\nversion: 1.0.0\ndescription: h\nlicense: MIT\ntargets: [claude]\n"
        "dependencies:\n  apm: []\n",
    )
    write(root / ".apm" / "skills" / "hs" / "SKILL.md", skill_md("hs"))
    hook = {
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "Write",
                    "hooks": [{"type": "command", "command": "./scripts/fmt.sh --check"}],
                }
            ]
        }
    }
    write(root / ".apm" / "hooks" / "fmt.json", json.dumps(hook))
    write(root / ".apm" / "hooks" / "scripts" / "fmt.sh", "#!/bin/sh\necho fmt\n")
    (root / ".claude").mkdir()

    world.apm(root, "install", "--no-policy")
    assert (root / ".claude" / "hooks" / "hooked" / "scripts" / "fmt.sh").is_file()

    world.apm(root, "pack", "-o", str(tmp_path / "out"))
    bundle = tmp_path / "out" / "hooked-1.0.0"
    packed_script = bundle / "hooks" / "scripts" / "fmt.sh"
    assert packed_script.read_text() == "#!/bin/sh\necho fmt\n"
    packed = json.loads((bundle / "hooks.json").read_text())
    command = packed["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
    assert command == '"${CLAUDE_PLUGIN_ROOT}/hooks/scripts/fmt.sh" --check', command
    bundle_files = load_yaml(bundle / "apm.lock.yaml")["pack"]["bundle_files"]
    assert "hooks/scripts/fmt.sh" in bundle_files

    # The packed command runs from a separate consumer checkout, as a host would
    # run it, with the plugin root pointing at the installed bundle.
    packed_script.chmod(0o755)
    consumer = world.isolated.work_root / "separate-consumer"
    consumer.mkdir()
    ran = subprocess.run(
        ["sh", "-c", command],
        cwd=consumer,
        env={**world.env, "CLAUDE_PLUGIN_ROOT": str(bundle)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert (ran.returncode, ran.stdout) == (0, "fmt\n"), ran.stderr


def _linked_producer(world: MarketplacePackWorld) -> Path:
    """A producer with own-source symlinks and a plugin hook with a script."""
    root = world.isolated.work_root / "linked"
    write(
        root / "apm.yml",
        "name: linked\nversion: 1.0.0\ndescription: l\nlicense: MIT\ntargets: [claude]\n"
        "dependencies:\n  apm: []\n",
    )
    skill = root / ".apm" / "skills" / "ls"
    write(skill / "SKILL.md", skill_md("ls"))
    write(skill / "real.md", "real\n")
    write(root / "docs" / "guide.md", "guide\n")
    (skill / "alias.md").symlink_to("real.md")
    (skill / "guide.md").symlink_to("../../../docs/guide.md")
    hook = {
        "hooks": {
            "PostToolUse": [
                {"matcher": "Write", "hooks": [{"type": "command", "command": "./scripts/fmt.sh"}]}
            ]
        }
    }
    write(root / ".apm" / "hooks" / "fmt.json", json.dumps(hook))
    write(root / ".apm" / "hooks" / "scripts" / "fmt.sh", "#!/bin/sh\necho fmt\n")
    (root / ".claude").mkdir()
    return root


def test_pack_names_each_dropped_source_symlink(tmp_path: Path, apm_binary_path: Path) -> None:
    """Own-source links are not followed (Atlas 2026-10-19/20) but never dropped silently."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    root = _linked_producer(world)
    world.apm(root, "install", "--no-policy")
    result = world.apm(root, "pack", "-o", str(tmp_path / "out"))
    text = output(result)
    for link in (".apm/skills/ls/alias.md", ".apm/skills/ls/guide.md"):
        assert f"Skipped symlinked source file {link}" in text, text
    bundle = tmp_path / "out" / "linked-1.0.0"
    assert (bundle / "skills" / "ls" / "real.md").is_file()
    assert not (bundle / "skills" / "ls" / "alias.md").exists()
    assert not (bundle / "skills" / "ls" / "guide.md").exists()
    assert "Skipped symlinked source file .apm/skills/ls/real.md" not in text


def test_bundle_install_skips_plugin_hooks_with_a_named_warning(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Local bundle installs never copy hooks.json raw or rename scripts to *.json."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    root = _linked_producer(world)
    world.apm(root, "install", "--no-policy")
    world.apm(root, "pack", "-o", str(tmp_path / "out"))
    bundle = tmp_path / "out" / "linked-1.0.0"
    assert (bundle / "hooks.json").is_file()
    assert (bundle / "hooks" / "scripts" / "fmt.sh").is_file()

    consumer = world.consumer("bundle-hooks-consumer", targets=("claude",))
    result = world.apm(consumer, "install", str(bundle), "--no-policy")
    text = output(result)
    for skipped in ("hooks.json", "hooks/scripts/fmt.sh"):
        assert f"Skipped packed bundle hook file '{skipped}'" in text, text
    assert not (consumer / ".claude" / "hooks.json").exists()
    assert list(consumer.rglob("*.sh.json")) == []
    assert not (consumer / ".claude" / "hooks").exists()
    assert (consumer / ".claude" / "skills" / "ls" / "SKILL.md").is_file()
    lock_text = (consumer / "apm.lock.yaml").read_text()
    assert "hooks" not in lock_text
    world.apm(consumer, "audit", "--ci", "--no-policy")
