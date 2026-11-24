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
_AGENT = "---\nname: helper\ndescription: Helper agent\n{hooks}---\nHelp.\n"
_PACKED = {
    "command": "commands/run.md",
    "skill": "skills/hs/SKILL.md",
    "agent": "agents/helper.agent.md",
    "malformed": "commands/run.md",
}
_DEPLOYED = {
    "command": ".claude/commands/run.md",
    "skill": ".claude/skills/hs/SKILL.md",
    "agent": ".claude/agents/helper.md",
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
    write(producer / ".apm" / "agents" / "helper.agent.md", _AGENT.format(hooks=""))
    (producer / ".claude").mkdir()
    world.apm(producer, "install", "--no-policy")
    world.apm(producer, "pack", "-o", str(tmp_path / "out"))
    bundle = tmp_path / "out" / "fmpkg-1.0.0"
    if hooked is not None:
        text = {
            "command": _COMMAND.format(hooks=_HOOKS),
            "skill": _SKILL.format(hooks=_HOOKS),
            "agent": _AGENT.format(hooks=_HOOKS),
            "malformed": _MALFORMED,
            "malformed-agent": _MALFORMED,
            "undecodable-agent": "---\nname: helper\n---\n\udcff\n",
        }[hooked]
        rel = _PACKED["agent" if hooked.endswith("-agent") else hooked]
        packed = bundle / rel
        packed.write_bytes(text.encode("utf-8", errors="surrogateescape"))
        lock = load_yaml(bundle / "apm.lock.yaml")
        lock["pack"]["bundle_files"][rel] = hashlib.sha256(packed.read_bytes()).hexdigest()
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
@pytest.mark.parametrize("hooked", ["command", "skill", "agent", "malformed"])
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
    pending = world.apm(consumer, "approve", "--pending")
    commands = re.findall(r"apm approve '([^']+)'", pending.stdout)
    assert commands == [key], pending.stdout
    approved = output(world.apm(consumer, "approve", commands[0]))
    assert "[+] Approved local bundle 'fmpkg': hooks" in approved, approved
    before = tree_bytes(consumer / ".claude")
    world.apm(consumer, "install", str(bundle), "--no-policy", "--dry-run")
    assert tree_bytes(consumer / ".claude") == before

    world.apm(consumer, "install", str(bundle), "--no-policy")

    pending = output(world.apm(consumer, "approve", "--pending"))
    assert "All packages with executables are approved." in pending, pending
    deployed = consumer / _DEPLOYED[hooked]
    assert deployed.read_bytes() == (bundle / _PACKED[hooked]).read_bytes()
    assert _DEPLOYED[hooked] in world.lock(consumer)["local_deployed_files"]
    world.apm(consumer, "audit", "--ci", "--no-policy")


def test_bundle_markdown_without_frontmatter_hooks_deploys_unapproved(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    bundle, _key = _bundle(world, tmp_path, None)
    consumer = _consumer(world, "plain-consumer")

    text = output(world.apm(consumer, "install", str(bundle), "--no-policy"))

    assert "Skipped packed bundle hooks file" not in text, text
    for kind in ("command", "skill", "agent"):
        assert (consumer / _DEPLOYED[kind]).read_bytes() == (bundle / _PACKED[kind]).read_bytes()


@pytest.mark.parametrize(
    ("hooked", "reported"),
    [
        pytest.param(
            "agent", "Skipped packed bundle hooks file 'agents/helper.agent.md'", id="hooks"
        ),
        # A rendered agent's frontmatter is the renderer's to judge: invalid YAML
        # is ignored with its warning (so no hooks reach the TOML), undecodable
        # bytes are skipped with the renderer's reason. Neither is reported as hooks.
        pytest.param("malformed-agent", "invalid YAML frontmatter was ignored", id="bad-yaml"),
        pytest.param(
            "undecodable-agent",
            "Skipped packed bundle agent 'agents/helper.agent.md'",
            id="bad-bytes",
        ),
    ],
)
def test_rendered_bundle_agent_frontmatter_is_judged_by_hooks_then_renderer(
    tmp_path: Path, apm_binary_path: Path, hooked: str, reported: str
) -> None:
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    bundle, _key = _bundle(world, tmp_path, hooked)
    consumer = world.consumer("codex-consumer", targets=("codex",))
    manifest = load_yaml(consumer / "apm.yml")
    manifest["executables"] = {}
    dump_yaml(manifest, consumer / "apm.yml")

    text = output(world.apm(consumer, "install", str(bundle), "--no-policy"))

    assert reported in text, text
    rendered = consumer / ".codex" / "agents" / "helper.toml"
    if hooked == "malformed-agent":
        assert "Skipped packed bundle hooks file" not in text, text
        assert "echo bundled" not in rendered.read_text(encoding="utf-8")
        assert ".codex/agents/helper.toml" in world.lock(consumer)["local_deployed_files"]
    else:
        assert not rendered.exists()
        lock = world.lock(consumer) if (consumer / "apm.lock.yaml").exists() else {}
        assert ".codex/agents/helper.toml" not in ((lock or {}).get("local_deployed_files") or [])


_RULE = "---\napplyTo: '**'\ndescription: Style\n---\nUse tabs.\n"
_BAD_RULES = {
    "bad-utf8": b"---\napplyTo: '**'\nnote: \xff\n---\nUse tabs.\n",
    "bad-yaml": b"---\napplyTo: [unclosed\n---\nUse tabs.\n",
}


@pytest.mark.parametrize("approved", [False, True], ids=["unapproved", "approved"])
@pytest.mark.parametrize("bad", sorted(_BAD_RULES))
def test_converted_instruction_with_unreadable_frontmatter_deploys_nothing(
    tmp_path: Path, apm_binary_path: Path, bad: str, approved: bool
) -> None:
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    producer = world.isolated.work_root / "rulepkg"
    write(
        producer / "apm.yml",
        "name: rulepkg\nversion: 1.0.0\ndescription: r\nlicense: MIT\ntargets: [claude]\n"
        "dependencies:\n  apm: []\n",
    )
    write(producer / ".apm" / "instructions" / "style.instructions.md", _RULE)
    (producer / ".claude").mkdir()
    world.apm(producer, "install", "--no-policy")
    world.apm(producer, "pack", "-o", str(tmp_path / "out"))
    bundle = tmp_path / "out" / "rulepkg-1.0.0"
    (packed,) = [
        rel
        for rel in load_yaml(bundle / "apm.lock.yaml")["pack"]["bundle_files"]
        if rel.endswith(".md")
    ]
    (bundle / packed).write_bytes(_BAD_RULES[bad])
    lock = load_yaml(bundle / "apm.lock.yaml")
    lock["pack"]["bundle_files"][packed] = hashlib.sha256(_BAD_RULES[bad]).hexdigest()
    dump_yaml(lock, bundle / "apm.lock.yaml")
    consumer = _consumer(world, "rule-consumer")
    if approved:
        info = detect_local_bundle(bundle)
        key = local_bundle_approval_key(
            info.package_id,
            str(info.plugin_json.get("version") or ""),
            info.source_dir,
            info.lockfile,
        )
        manifest = load_yaml(consumer / "apm.yml")
        manifest["executables"] = {"allow": {key: {"hooks": True}}}
        dump_yaml(manifest, consumer / "apm.yml")

    text = output(world.apm(consumer, "install", str(bundle), "--no-policy"))

    assert "Skipped" in text and "style" in text, text
    assert list((consumer / ".claude").rglob("style*")) == []
    lock_path = consumer / "apm.lock.yaml"
    rows = (
        (load_yaml(lock_path) or {}).get("local_deployed_files") or [] if lock_path.exists() else []
    )
    assert not any("style" in str(row) for row in rows), rows


def test_bundle_txt_command_with_frontmatter_hooks_is_parked(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A packed command lowered to Markdown is checked whatever its packed suffix."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    bundle, _key = _bundle(world, tmp_path, None)
    text_command = _COMMAND.format(hooks=_HOOKS).encode("utf-8")
    (bundle / "commands" / "run.txt").write_bytes(text_command)
    lock = load_yaml(bundle / "apm.lock.yaml")
    lock["pack"]["bundle_files"]["commands/run.txt"] = hashlib.sha256(text_command).hexdigest()
    dump_yaml(lock, bundle / "apm.lock.yaml")
    consumer = _consumer(world, "txt-consumer")

    text = output(world.apm(consumer, "install", str(bundle), "--no-policy"))

    assert "Skipped packed bundle hooks file 'commands/run.txt'" in text, text
    assert list((consumer / ".claude" / "commands").glob("run.txt*")) == []
    rows = world.lock(consumer).get("local_deployed_files") or []
    assert not any("run.txt" in str(row) for row in rows), rows


@pytest.mark.parametrize("withdrawal", ["revoked", "denied"])
def test_withdrawn_approval_retires_deployed_markdown_with_frontmatter_hooks(
    tmp_path: Path, apm_binary_path: Path, withdrawal: str
) -> None:
    """#5254: the next install retires hooked Markdown a withdrawn key admitted."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    bundle, key = _bundle(world, tmp_path, "command")
    consumer = _consumer(world, "retire-consumer")
    manifest = load_yaml(consumer / "apm.yml")
    manifest["executables"] = {"allow": {key: {"hooks": True}}}
    dump_yaml(manifest, consumer / "apm.yml")
    world.apm(consumer, "install", str(bundle), "--no-policy")
    hooked, plain = consumer / _DEPLOYED["command"], consumer / _DEPLOYED["skill"]
    assert hooked.is_file() and plain.is_file()
    before = tree_bytes(consumer / ".claude")

    manifest = load_yaml(consumer / "apm.yml")
    manifest["executables"] = {} if withdrawal == "revoked" else {"deny": {key: {"hooks": True}}}
    dump_yaml(manifest, consumer / "apm.yml")
    text = output(world.apm(consumer, "install", "--no-policy"))

    assert "Retired 1 Markdown file(s) with frontmatter hooks from local bundle 'fmpkg'" in text
    assert not hooked.exists()
    after = tree_bytes(consumer / ".claude")
    assert {rel: data for rel, data in before.items() if rel != "commands/run.md"} == after
    lock = world.lock(consumer)
    assert _DEPLOYED["command"] not in lock["local_deployed_files"]
    assert _DEPLOYED["skill"] in lock["local_deployed_files"]

    # Converged: the next install retires nothing more and changes no bytes.
    again = output(world.apm(consumer, "install", "--no-policy"))
    assert "Retired" not in again, again
    assert tree_bytes(consumer / ".claude") == after


def test_withdrawn_approval_keeps_hooked_markdown_edited_outside_apm(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    bundle, key = _bundle(world, tmp_path, "command")
    consumer = _consumer(world, "edited-consumer")
    manifest = load_yaml(consumer / "apm.yml")
    manifest["executables"] = {"allow": {key: {"hooks": True}}}
    dump_yaml(manifest, consumer / "apm.yml")
    world.apm(consumer, "install", str(bundle), "--no-policy")
    hooked = consumer / _DEPLOYED["command"]
    edited = hooked.read_bytes() + b"Edited by hand.\n"
    hooked.write_bytes(edited)

    manifest = load_yaml(consumer / "apm.yml")
    manifest["executables"] = {}
    dump_yaml(manifest, consumer / "apm.yml")
    text = output(world.apm(consumer, "install", "--no-policy"))

    assert f"Kept edited file(s): {_DEPLOYED['command']}" in text, text
    assert hooked.read_bytes() == edited
    assert _DEPLOYED["command"] in world.lock(consumer)["local_deployed_files"]
