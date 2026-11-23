"""The libyaml-backed lockfile loader keeps every guard of the bounded loader.

``utils/yaml_io.load_machine_yaml_str`` (lockfiles only) scans and parses in
C and composes and constructs with the pure loader's Python composer and
bounded constructor. These cells pin its equivalence with
``load_yaml_str``, its fail-closed bomb verdicts (same error class), that an
alias bomb is refused before any object is constructed, the private copies
handed out by the memoized lockfile parse, and its single caller.
"""

from __future__ import annotations

import random
import re
from pathlib import Path

import pytest
import yaml

from apm_cli.deps import lockfile_container
from apm_cli.utils import yaml_io

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-read-only-lockfile-path")]

ROOT = Path(__file__).resolve().parents[3]
_LOADERS = (yaml_io.load_yaml_str, yaml_io.load_machine_yaml_str)


def _alias_bomb(levels: int = 30) -> str:
    lines = ["l0: &l0 [x, x]"]
    lines += [f"l{i}: &l{i} [*l{i - 1}, *l{i - 1}]" for i in range(1, levels)]
    return "\n".join(lines) + "\n"


def _merge_bomb(levels: int = 40) -> str:
    lines = ["m0: &m0 {k0: v}"]
    lines += [f"m{i}: &m{i} {{<<: [*m{i - 1}, *m{i - 1}], k{i}: v}}" for i in range(1, levels)]
    return "\n".join(lines) + "\n"


BOMBS = {
    "alias-billion-laughs": _alias_bomb(),
    "merge-key-chain": _merge_bomb(),
    "huge-int": "n: " + "9" * 6000 + "\n",
    "deep-flow-nesting": "[" * 5000 + "]" * 5000 + "\n",
    "deep-block-nesting": "- " * 5000 + "x\n",
}


@pytest.mark.parametrize("name", sorted(BOMBS))
def test_every_bomb_fails_closed_with_the_pure_loaders_error(name: str) -> None:
    verdicts = []
    for load in _LOADERS:
        with pytest.raises(yaml.YAMLError) as caught:
            load(BOMBS[name])
        verdicts.append(type(caught.value))
    assert verdicts[0] is verdicts[1], verdicts


def test_an_alias_bomb_is_refused_before_any_object_is_built(monkeypatch) -> None:
    built: list[object] = []
    original = yaml.constructor.BaseConstructor.construct_object

    def counting(self, node, deep=False):
        built.append(node)
        return original(self, node, deep=deep)

    monkeypatch.setattr(yaml.constructor.BaseConstructor, "construct_object", counting)
    with pytest.raises(yaml.YAMLError):
        yaml_io.load_machine_yaml_str(_alias_bomb())
    assert built == [], "the expansion guard must run on the composed graph first"


def test_an_oversized_document_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(yaml_io, "_MAX_MACHINE_YAML_CHARS", 64)
    with pytest.raises(yaml.YAMLError, match="character budget"):
        yaml_io.load_machine_yaml_str("k: " + "v" * 100 + "\n")


_LOCKFILES = sorted((ROOT / "tests" / "fixtures").rglob("*.lock.yaml"))


@pytest.mark.parametrize("path", _LOCKFILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_lockfile_fixture_parses_identically(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert yaml_io.load_machine_yaml_str(text) == yaml_io.load_yaml_str(text)


def _random_tree(rng: random.Random, depth: int = 0) -> object:
    kind = rng.randrange(6 if depth < 4 else 4)
    scalars = (
        lambda: rng.choice(["", "x", "a b", "k: v", "- y", "#c", "*a", "&b", "yes", "1.5", "007"]),
        lambda: rng.randrange(-(10**12), 10**12),
        lambda: rng.choice([True, False, None, 0.25]),
        lambda: "".join(rng.choice("ab:-# \t'\"[]{}") for _ in range(rng.randrange(8))),
    )
    if kind < 4:
        return scalars[kind]()
    if kind == 4:
        return [_random_tree(rng, depth + 1) for _ in range(rng.randrange(4))]
    return {f"k{i}": _random_tree(rng, depth + 1) for i in range(rng.randrange(4))}


@pytest.mark.parametrize("seed", range(200))
def test_a_dumped_document_round_trips_identically(seed: int) -> None:
    data = {"root": _random_tree(random.Random(seed))}  # noqa: S311 -- seeded test data
    text = yaml_io.yaml_to_str(data)
    assert yaml_io.load_machine_yaml_str(text) == yaml_io.load_yaml_str(text) == data


def test_a_mutated_copy_never_reaches_the_memoized_parse() -> None:
    text = "lockfile_version: '1'\ndependencies:\n- repo_url: o/r\n  deployed_files: [a]\n"
    first = lockfile_container.parse_lockfile_text(text)
    first["dependencies"][0]["deployed_files"].append("tampered")
    first["dependencies"].clear()
    again = lockfile_container.parse_lockfile_text(text)
    assert again == yaml_io.load_yaml_str(text)


def test_only_the_lockfile_parse_uses_the_machine_loader() -> None:
    callers = sorted(
        str(path.relative_to(ROOT))
        for path in (ROOT / "src").rglob("*.py")
        if re.search(r"\bload_machine_yaml_str\b", path.read_text(encoding="utf-8"))
    )
    assert callers == [
        "src/apm_cli/deps/lockfile_container.py",
        "src/apm_cli/utils/yaml_io.py",
    ]
