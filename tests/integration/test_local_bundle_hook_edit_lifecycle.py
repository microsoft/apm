"""APMLifecycle: an edited bundle hook entry never goes silently invisible (#5078).

PROMISE: P-OWN-local-bundle-hook-trust, P-INV-I7 -- nothing is skipped silently: a local
bundle's merged hook entry the user edited (for example its matcher) no longer
matches APM's recorded content, and an unreadable or missing ownership record
hides every entry; ``apm audit --ci`` names each case instead of losing track
of the bundle's hooks (source: issue #5078 accepted scope and review).
CELL: OP=install,audit SRC=local-bundle PRIM=hooks SCOPE=project TGT=claude
CHANGE=none|matcher|sidecar-corrupt|sidecar-missing
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


_EXPECTED = {
    "unchanged": None,
    "matcher-edited": "hookpkg: a deployed hook entry was changed outside APM",
    "marker-copied": "hookpkg: a deployed hook entry was changed outside APM",
    "sidecar-corrupt": ".claude/settings.json: the hook config or its ownership record cannot be read",
    "sidecar-missing": "hookpkg: .claude/settings.json has no ownership record",
}


@pytest.mark.parametrize("change", list(_EXPECTED))
def test_audit_names_bundle_hooks_apm_can_no_longer_attribute(
    tmp_path: Path, apm_binary_path: Path, change: str
) -> None:
    """An approved bundle hook passes audit; an edited entry, or an unreadable or missing
    ownership record, fails it naming what APM can no longer attribute (#5078)."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    bundle, key = _hooked_bundle(world, tmp_path)
    consumer = world.consumer("consumer", targets=("claude",))
    manifest = load_yaml(consumer / "apm.yml")
    manifest["executables"] = {"allow": {key: {"hooks": True}}}
    dump_yaml(manifest, consumer / "apm.yml")
    world.apm(consumer, "install", str(bundle))
    settings_path = consumer / ".claude" / "settings.json"
    sidecar = consumer / ".claude" / "apm-hooks.json"
    settings = json.loads(settings_path.read_text("utf-8"))
    assert [e["matcher"] for e in settings["hooks"]["PreToolUse"]] == ["Bash"]
    assert sidecar.is_file()
    if change in ("matcher-edited", "marker-copied"):
        settings["hooks"]["PreToolUse"][0]["matcher"] = "Bash|Write"
        if change == "marker-copied":
            recorded = json.loads(sidecar.read_text("utf-8"))["PreToolUse"][0]
            settings["hooks"]["PreToolUse"][0]["_apm_source"] = recorded["_apm_source"]
        settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    elif change == "sidecar-corrupt":
        sidecar.write_text("{not json", encoding="utf-8")
    elif change == "sidecar-missing":
        sidecar.unlink()
    before = settings_path.read_bytes()

    audit = world.apm(consumer, "audit", "--ci", "--no-policy", expect=None)

    text = output(audit)
    if _EXPECTED[change] is None:
        assert audit.returncode == 0, text
    else:
        assert audit.returncode == 1, text
        assert _EXPECTED[change] in text, text
    assert settings_path.read_bytes() == before
