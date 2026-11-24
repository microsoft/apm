"""A non-string frontmatter key is the loader's named YAML error (#5028).

PROMISE: P-INV-I7 -- nothing fails with a bare crash: a frontmatter key that
is not a string (``1:``, an int too large to render, a complex or binary key)
reaches every caller as the frontmatter loader's own YAML error, naming the
key in printable ASCII, so each primitive applies its EXISTING
malformed-frontmatter policy (source: #5028 scope record, QL ruling):

* command: that file is skipped with a warning, its sibling deploys (#4971);
* agent: deploys with a parse warning, its sibling deploys;
* instruction: its package fails, with no partial writes;
* root ``SKILL.md`` package: invalid, with no partial writes;
* package skill: copied verbatim; its frontmatter is not parsed at install.

A per-file skip for instructions and skills is a policy change (#5216).
CELL: OP=install SRC=local PRIM={command,agent,instruction,skill-package,
package-skill} SCOPE=project FRONTMATTER={int,int-too-large,complex,binary-esc}
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.integration._install_audit_replay_kit import ReplayWorld, tree_digest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I7"),
]

# key id -> (frontmatter lines, text the diagnostic must carry)
_KEYS = {
    "int": ("1: x", "non-string frontmatter key(s): 1"),
    # A hex int this long renders past Python's digit limit.
    "int-too-large-to-render": (
        "? 0x" + "f" * 5000 + "\n: x",
        "non-string frontmatter key(s): <int>",
    ),
    "complex": ("? [a, b]\n: x", "found unhashable key"),
    # base64 of ESC "[31mred": the key names itself without a raw control byte.
    "binary-esc": (
        "? !!binary G1szMW1yZWQ=\n: x",
        "non-string frontmatter key(s): b'\\x1b[31mred'",
    ),
}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _front(name: str, extra: str, *fields: str) -> str:
    body = "".join(f"{field}\n" for field in fields)
    return f"---\n{extra}{body}description: {name} fixture\n---\n# {name}\n"


def _package(world: ReplayWorld, primitive: str, line: str) -> list[str]:
    """Write the package for *primitive* with one bad and one good file; return its deps."""
    if primitive == "skill-package":
        _write(world.project / "vendor/s/SKILL.md", _front("s", line + "\n", "name: s"))
        return ["./vendor/s"]
    root = world.project / "vendor" / "p"
    _write(root / "apm.yml", "name: p\nversion: 1.0.0\n")
    for name, extra in (("bad-one", line + "\n"), ("good-one", "")):
        if primitive == "command":
            _write(root / f".apm/prompts/{name}.prompt.md", _front(name, extra))
        elif primitive == "agent":
            _write(root / f".apm/agents/{name}.agent.md", _front(name, extra, f"name: {name}"))
        elif primitive == "instruction":
            path = root / f".apm/instructions/{name}.instructions.md"
            _write(path, _front(name, extra, "applyTo: '**'"))
        else:
            _write(root / f".apm/skills/{name}/SKILL.md", _front(name, extra, f"name: {name}"))
    return ["./vendor/p"]


_DEPLOYED = {
    "command": ".claude/commands/{}.md",
    "agent": ".claude/agents/{}.md",
    "instruction": ".claude/rules/{}.md",
    "package-skill": ".claude/skills/{}/SKILL.md",
}


@pytest.mark.parametrize("key", list(_KEYS))
@pytest.mark.parametrize(
    "primitive", ["command", "agent", "instruction", "skill-package", "package-skill"]
)
def test_a_non_string_frontmatter_key_follows_the_primitive_policy(
    tmp_path: Path, apm_binary_path: Path, primitive: str, key: str
) -> None:
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    line, named = _KEYS[key]
    deps = _package(world, primitive, line)
    (world.project / ".claude").mkdir()
    dump_yaml(
        {"name": "proj", "version": "1.0.0", "targets": ["claude"], "dependencies": {"apm": deps}},
        world.project / "apm.yml",
    )
    before = tree_digest(world.project)

    result = world.apm("install", expect=None)

    output = result.stdout + result.stderr
    assert "keywords must be strings" not in output, output
    assert "TypeError" not in output, output
    assert "\x1b" not in output, output
    if primitive in ("instruction", "skill-package"):
        # Fail closed: the package fails by name and nothing of it is written.
        assert result.returncode == 1, output
        assert named in output, output
        assert tree_digest(world.project) == before, output
        assert not (world.project / "apm.lock.yaml").exists(), output
        return
    assert result.returncode == 0, output
    deployed = world.project / _DEPLOYED[primitive].format("good-one")
    assert deployed.is_file(), output
    if primitive == "command":
        assert named in output and "bad-one" in output, output
        assert not (world.project / _DEPLOYED["command"].format("bad-one")).exists(), output
    elif primitive == "agent":
        assert named in output and "bad-one.agent.md" in output, output
    else:
        # Copied verbatim: a package skill's frontmatter is not parsed at install.
        assert (world.project / _DEPLOYED[primitive].format("bad-one")).is_file(), output
