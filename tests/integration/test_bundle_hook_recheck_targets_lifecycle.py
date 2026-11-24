"""Bundle hook re-check after deny or revoke covers every hook target of the scope.

A local bundle installed with an explicit ``--target`` outside the manifest's
targets (or at user scope, or under a redirected ``CLAUDE_CONFIG_DIR``)
registers hooks in a target the manifest does not name. When its approval is
denied or revoked, the next ``apm install`` retires those hooks and
``apm audit --ci`` reports them first, as for a manifest target.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apm_cli.bundle.local_bundle import detect_local_bundle
from apm_cli.security.executables import local_bundle_approval_key
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._marketplace_pack_kit import MarketplacePackWorld, output, skill_md, write

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
    pytest.mark.e2e,
    pytest.mark.promise("P-OWN-local-bundle-hook-trust"),
]


def _packed_bundle(world: MarketplacePackWorld, tmp_path: Path) -> tuple[Path, str]:
    producer = world.isolated.work_root / "hookpkg"
    write(
        producer / "apm.yml",
        "name: hookpkg\nversion: 1.0.0\ndescription: h\nlicense: MIT\ntargets: [claude]\n"
        "dependencies:\n  apm: []\n",
    )
    write(producer / ".apm" / "skills" / "hs" / "SKILL.md", skill_md("hs"))
    hook = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "./scripts/x.sh"}]}
            ]
        }
    }
    write(producer / ".apm" / "hooks" / "hooks.json", json.dumps(hook))
    write(producer / ".apm" / "hooks" / "scripts" / "x.sh", "#!/bin/sh\necho hi\n").chmod(0o755)
    (producer / ".claude").mkdir()
    world.apm(producer, "install", "--no-policy")
    world.apm(producer, "pack", "-o", str(tmp_path / "out"))
    bundle = tmp_path / "out" / "hookpkg-1.0.0"
    info = detect_local_bundle(bundle)
    key = local_bundle_approval_key(
        info.package_id, str(info.plugin_json.get("version") or ""), info.source_dir, info.lockfile
    )
    return bundle, key


def _matchers(claude_root: Path) -> list[str]:
    settings = claude_root / "settings.json"
    if not settings.exists():
        return []
    hooks = json.loads(settings.read_text(encoding="utf-8")).get("hooks", {})
    return [entry["matcher"] for entry in hooks.get("PreToolUse", [])]


def _assert_no_bundle_hook(claude_root: Path) -> None:
    """The bundle's PreToolUse entry is gone from the target's settings.json."""
    assert _matchers(claude_root) == []


def _set_executables(manifest_path: Path, executables: dict) -> None:
    manifest = load_yaml(manifest_path)
    manifest["executables"] = executables
    dump_yaml(manifest, manifest_path)


@pytest.mark.parametrize("withdrawal", ["deny", "revoke"])
@pytest.mark.parametrize("scope", ["project-target-flag", "user", "user-claude-config-dir"])
def test_hook_recheck_after_withdrawal_covers_targets_outside_the_manifest(
    tmp_path: Path, apm_binary_path: Path, scope: str, withdrawal: str
) -> None:
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    bundle, key = _packed_bundle(world, tmp_path)
    home = world.isolated.home
    if scope == "project-target-flag":
        cwd = world.consumer("consumer", targets=("copilot",))
        manifest_path, flags, claude_root = cwd / "apm.yml", (), cwd / ".claude"
        claude_root.mkdir()
    else:
        cwd = world.isolated.work_root
        manifest_path, flags = home / ".apm" / "apm.yml", ("-g",)
        write(manifest_path, "name: user\nversion: 0.1.0\ntargets: [copilot]\n")
        (home / ".copilot").mkdir(exist_ok=True)
        claude_root = home / ".claude"
        if scope == "user-claude-config-dir":
            claude_root = home / "profiles" / "work"
            world.env["CLAUDE_CONFIG_DIR"] = str(claude_root)
        claude_root.mkdir(parents=True, exist_ok=True)
    _set_executables(manifest_path, {"allow": {key: {"hooks": True}}})
    world.apm(cwd, "install", *flags, "--target", "claude", str(bundle), "--no-policy")
    assert _matchers(claude_root) == ["Bash"]

    if withdrawal == "deny":
        world.apm(cwd, "deny", *flags, key)
    else:
        _set_executables(manifest_path, {})
    # Audit runs in the scope's metadata workspace: the project, or $HOME/.apm.
    audit = world.apm(
        manifest_path.parent, "audit", "--ci", "--no-policy", "--no-fail-fast", expect=None
    )
    assert audit.returncode == 1
    assert "Local bundle hooks are approved" not in output(audit)
    assert "hookpkg:" in audit.stdout + audit.stderr, output(audit)
    installed = world.apm(cwd, "install", *flags, "--no-policy")

    assert installed.returncode == 0, output(installed)
    assert "Retired hooks from local bundle 'hookpkg'" in installed.stdout + installed.stderr
    _assert_no_bundle_hook(claude_root)
    audit = world.apm(
        manifest_path.parent, "audit", "--ci", "--no-policy", "--no-fail-fast", expect=None
    )
    assert "Local bundle hooks are approved" in output(audit), output(audit)
    if scope != "user-claude-config-dir":
        # Under a redirected CLAUDE_CONFIG_DIR, deployed-files-present fails on its own.
        assert audit.returncode == 0, output(audit)


def test_hook_recheck_leaves_a_linked_outside_target_untouched(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A target root the manifest does not name and that links outside is not re-checked."""
    import os

    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    bundle, key = _packed_bundle(world, tmp_path)
    other = world.consumer("other-project", targets=("claude",))
    _set_executables(other / "apm.yml", {"allow": {key: {"hooks": True}}})
    world.apm(other, "install", str(bundle), "--no-policy")
    assert _matchers(other / ".claude") == ["Bash"]
    consumer = world.consumer("consumer", targets=("copilot",))
    os.symlink(other / ".claude", consumer / ".claude", target_is_directory=True)
    before = {p.name: p.read_bytes() for p in (other / ".claude").iterdir() if p.is_file()}

    installed = world.apm(consumer, "install", "--no-policy")
    audit = world.apm(consumer, "audit", "--ci", "--no-policy", expect=None)

    assert installed.returncode == 0, output(installed)
    assert "Retired hooks" not in installed.stdout + installed.stderr
    assert "hookpkg" not in audit.stdout + audit.stderr, output(audit)
    after = {p.name: p.read_bytes() for p in (other / ".claude").iterdir() if p.is_file()}
    assert after == before
