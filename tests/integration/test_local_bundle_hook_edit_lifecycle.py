"""APMLifecycle: an edited bundle hook entry never goes silently invisible (#5078).

PROMISE: P-OWN-local-bundle-hook-trust, P-INV-I7 -- nothing is skipped silently: a local bundle's merged hook
entry the user edited (for example its matcher) no longer matches APM's
recorded content, so ``apm audit --ci`` names the bundle as changed outside
APM instead of losing track of the entry (source: issue #5078 accepted scope).
CELL: OP=install,audit SRC=local-bundle PRIM=hooks SCOPE=project TGT=claude
EDIT=none|matcher
"""

from __future__ import annotations

import json
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
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise("P-OWN-local-bundle-hook-trust", "P-INV-I7"),
]


def _hooked_bundle(world: MarketplacePackWorld, tmp_path: Path) -> tuple[Path, str]:
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
    write(producer / ".apm" / "hooks" / "scripts" / "x.sh", "#!/bin/sh\necho h\n").chmod(0o755)
    (producer / ".claude").mkdir()
    world.apm(producer, "install", "--no-policy")
    world.apm(producer, "pack", "-o", str(tmp_path / "out"))
    bundle = tmp_path / "out" / "hookpkg-1.0.0"
    info = detect_local_bundle(bundle)
    key = local_bundle_approval_key(
        info.package_id, str(info.plugin_json.get("version") or ""), info.source_dir, info.lockfile
    )
    return bundle, key


@pytest.mark.parametrize("edited", [False, True], ids=["unchanged", "matcher-edited"])
def test_audit_names_a_bundle_hook_entry_edited_outside_apm(
    tmp_path: Path, apm_binary_path: Path, edited: bool
) -> None:
    """An approved bundle hook passes audit; after a matcher edit audit names the bundle."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    bundle, key = _hooked_bundle(world, tmp_path)
    consumer = world.consumer("consumer", targets=("claude",))
    manifest = load_yaml(consumer / "apm.yml")
    manifest["executables"] = {"allow": {key: {"hooks": True}}}
    dump_yaml(manifest, consumer / "apm.yml")
    world.apm(consumer, "install", str(bundle))
    settings_path = consumer / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text("utf-8"))
    assert [e["matcher"] for e in settings["hooks"]["PreToolUse"]] == ["Bash"]
    if edited:
        settings["hooks"]["PreToolUse"][0]["matcher"] = "Bash|Write"
        settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    before = settings_path.read_bytes()

    audit = world.apm(consumer, "audit", "--ci", "--no-policy", expect=None)

    text = output(audit)
    if edited:
        assert audit.returncode == 1, text
        assert "hookpkg" in text and "changed outside APM" in text, text
    else:
        assert audit.returncode == 0, text
    assert settings_path.read_bytes() == before
