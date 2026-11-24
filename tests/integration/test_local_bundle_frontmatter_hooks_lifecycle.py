"""Bundle Markdown frontmatter hooks follow the bundle hook approval (#5248).

A local bundle's command ``.md`` or ``SKILL.md`` whose frontmatter declares
``hooks`` is parked like a bundle ``hooks/`` file: ``apm install <bundle>``
skips it and names it until the bundle's exact-content key is approved, then
deploys it. Markdown without frontmatter ``hooks`` deploys as before.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from apm_cli.bundle.local_bundle import detect_local_bundle
from apm_cli.security.executables import local_bundle_approval_key
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._marketplace_pack_kit import MarketplacePackWorld, output, tree_bytes, write

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
    pytest.mark.e2e,
    pytest.mark.promise("P-OWN-local-bundle-hook-trust"),
]

_HOOKS = (
    "hooks:\n  PreToolUse:\n    - matcher: Bash\n      hooks:\n"
    "        - type: command\n          command: echo bundled\n"
)
_COMMAND = "---\ndescription: Run it\n{hooks}---\nRun.\n"
_MALFORMED = "---\ndescription: [unclosed\n---\nRun.\n"
_SKILL = "---\nname: hs\ndescription: Skill hs\n{hooks}---\n# hs\n"
_PACKED = {
    "command": "commands/run.md",
    "skill": "skills/hs/SKILL.md",
    "malformed": "commands/run.md",
}
_DEPLOYED = {
    "command": ".claude/commands/run.md",
    "skill": ".claude/skills/hs/SKILL.md",
    "malformed": ".claude/commands/run.md",
}


def _bundle(world: MarketplacePackWorld, tmp_path: Path, hooked: str | None) -> tuple[Path, str]:
    """Pack a command and a skill; give *hooked*'s packed file frontmatter hooks."""
    producer = world.isolated.work_root / "fmpkg"
    write(
        producer / "apm.yml",
        "name: fmpkg\nversion: 1.0.0\ndescription: f\nlicense: MIT\ntargets: [claude]\n"
        "dependencies:\n  apm: []\n",
    )
    write(producer / ".apm" / "prompts" / "run.prompt.md", _COMMAND.format(hooks=""))
    write(producer / ".apm" / "skills" / "hs" / "SKILL.md", _SKILL.format(hooks=""))
    (producer / ".claude").mkdir()
    world.apm(producer, "install", "--no-policy")
    world.apm(producer, "pack", "-o", str(tmp_path / "out"))
    bundle = tmp_path / "out" / "fmpkg-1.0.0"
    if hooked is not None:
        text = {
            "command": _COMMAND.format(hooks=_HOOKS),
            "skill": _SKILL.format(hooks=_HOOKS),
            "malformed": _MALFORMED,
        }[hooked]
        packed = write(bundle / _PACKED[hooked], text)
        lock = load_yaml(bundle / "apm.lock.yaml")
        lock["pack"]["bundle_files"][_PACKED[hooked]] = hashlib.sha256(
            packed.read_bytes()
        ).hexdigest()
        dump_yaml(lock, bundle / "apm.lock.yaml")
    info = detect_local_bundle(bundle)
    key = local_bundle_approval_key(
        info.package_id, str(info.plugin_json.get("version") or ""), info.source_dir, info.lockfile
    )
    return bundle, key


def _consumer(world: MarketplacePackWorld, name: str) -> Path:
    consumer = world.consumer(name, targets=("claude",))
    manifest = load_yaml(consumer / "apm.yml")
    manifest["executables"] = {}
    dump_yaml(manifest, consumer / "apm.yml")
    return consumer


# Frontmatter that cannot be parsed counts as declaring hooks (fail closed).
@pytest.mark.parametrize("hooked", ["command", "skill", "malformed"])
def test_bundle_markdown_with_frontmatter_hooks_is_parked_until_approved(
    tmp_path: Path, apm_binary_path: Path, hooked: str
) -> None:
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    bundle, key = _bundle(world, tmp_path, hooked)
    consumer = _consumer(world, f"{hooked}-consumer")
    other = "command" if hooked == "skill" else "skill"

    text = output(world.apm(consumer, "install", str(bundle), "--no-policy"))

    assert f"Skipped packed bundle hooks file '{_PACKED[hooked]}'" in text, text
    assert not (consumer / _DEPLOYED[hooked]).exists()
    assert (consumer / _DEPLOYED[other]).is_file()
    assert _DEPLOYED[hooked] not in world.lock(consumer).get("local_deployed_files", [])
    printed = re.search(r"executables: allow: \"([^\"]+)\": hooks: true", text)
    assert printed is not None, text
    assert re.sub(r"\s+", "", printed.group(1)) == key

    manifest = load_yaml(consumer / "apm.yml")
    manifest["executables"] = {"allow": {key: {"hooks": True}}}
    dump_yaml(manifest, consumer / "apm.yml")
    before = tree_bytes(consumer / ".claude")
    world.apm(consumer, "install", str(bundle), "--no-policy", "--dry-run")
    assert tree_bytes(consumer / ".claude") == before

    world.apm(consumer, "install", str(bundle), "--no-policy")

    deployed = consumer / _DEPLOYED[hooked]
    assert deployed.read_bytes() == (bundle / _PACKED[hooked]).read_bytes()
    assert _DEPLOYED[hooked] in world.lock(consumer)["local_deployed_files"]


def test_bundle_markdown_without_frontmatter_hooks_deploys_unapproved(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    bundle, _key = _bundle(world, tmp_path, None)
    consumer = _consumer(world, "plain-consumer")

    text = output(world.apm(consumer, "install", str(bundle), "--no-policy"))

    assert "Skipped packed bundle hooks file" not in text, text
    for kind in ("command", "skill"):
        assert (consumer / _DEPLOYED[kind]).read_bytes() == (bundle / _PACKED[kind]).read_bytes()
