"""The libyaml-backed lockfile loader keeps every guard of the bounded loader.

``utils/yaml_io.load_machine_yaml_str`` (lockfiles only) scans and parses in
C and composes and constructs with the pure loader's Python composer and
bounded constructor. These cells pin its equivalence with
``load_yaml_str``, its fail-closed bomb verdicts (same error class), that an
alias bomb is refused before any object is constructed, the private copies
handed out by the memoized lockfile parse, and its single caller.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from pathlib import Path

import pytest
import yaml

from apm_cli.deps import lockfile_container
from apm_cli.utils import yaml_io

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-read-only-lockfile-path")]

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


def test_an_oversized_document_is_refused() -> None:
    oversized = "k: " + "v" * yaml_io._MAX_MACHINE_YAML_CHARS + "\n"
    with pytest.raises(yaml.YAMLError, match="character budget"):
        yaml_io.load_machine_yaml_str(oversized)


def test_the_composer_is_python_not_libyaml() -> None:
    """CParser's recursive C composer is never reached (QL car A condition 1)."""
    composer = yaml.composer.Composer
    loader = yaml_io._BoundedCSafeLoader
    for name in ("get_single_node", "compose_document", "compose_node", "check_node", "get_node"):
        assert getattr(loader, name) is getattr(composer, name), name


def test_every_lockfile_read_uses_the_memo_key_owner() -> None:
    """Seeding and reading the parse memo share one key (and so one loader)."""
    text = "lockfile_version: '1'\ndependencies: []\n"
    assert lockfile_container.parse_key(text) == (yaml_io.load_machine_yaml_str, text)


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
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "src").rglob("*.py")
        if re.search(r"\bload_machine_yaml_str\b", path.read_text(encoding="utf-8"))
    )
    assert callers == [
        "src/apm_cli/deps/lockfile_container.py",
        "src/apm_cli/utils/yaml_io.py",
    ]


@pytest.mark.parametrize("path", _LOCKFILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_a_lockfile_identity_is_unchanged_by_the_owner_parse(path: Path) -> None:
    """Journals an older APM wrote compare equal: the identity formula's value is the same."""
    import hashlib
    import json

    from apm_cli.install.deploy_snapshot import lockfile_identity

    text = path.read_text(encoding="utf-8")
    try:
        canonical = json.dumps(yaml_io.load_yaml_str(text), sort_keys=True, default=str)
    except (yaml.YAMLError, ValueError, TypeError, RecursionError):
        canonical = text.replace("\r\n", "\n")
    assert lockfile_identity(text) == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_identifying_one_lockfile_twice_parses_it_once() -> None:
    """The commit mark and the settle identify the same text: one memoized parse."""
    from apm_cli.install.deploy_snapshot import lockfile_identity

    text = "lockfile_version: '1'\ndependencies:\n- repo_url: o/identity-memo\n"
    key = lockfile_container.parse_key(text)
    lockfile_container._parsed_texts.pop(key, None)
    first = lockfile_identity(text)
    parsed = lockfile_container._parsed_texts[key]
    assert lockfile_identity(text) == first
    assert lockfile_container._parsed_texts[key] is parsed


@pytest.mark.parametrize(
    "text",
    [
        'lockfile_version: "1"\nname: "\\ud800"\n',
        'lockfile_version: "1"\nx-vendor: !!set {0: null, 36: null, 72: null, 108: null}\n',
    ],
    ids=["lone-surrogate", "set-value"],
)
def test_a_lockfile_the_owner_refuses_keeps_its_old_identity(text: str) -> None:
    """Refused or non-JSON-native text: the old loader's identity, so replay stays committed."""
    from apm_cli.install.deploy_snapshot import lockfile_identity

    canonical = json.dumps(yaml_io.load_yaml_str(text), sort_keys=True, default=str)
    assert lockfile_identity(text) == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("path", _LOCKFILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_lockfile_fixture_emits_the_same_bytes_from_libyaml(path: Path) -> None:
    data = yaml_io.load_yaml_str(path.read_text(encoding="utf-8"))
    assert yaml_io.machine_yaml_to_str(data) == yaml_io.yaml_to_str(data)


def _random_emit_tree(rng: random.Random, depth: int = 0) -> object:
    words = [
        "sha256:" + "0f" * 32,
        "https://github.com/" + "o" * 40 + "/r.git#" + "a" * 40,
        "-x:" + "y" * 100,
        "'q" + "z" * 80,
        "a",
        "b c",
        "\u00e9t\u00e9",
        "\u4e2d\u6587",
        "x" * 90,
        "line\nbreak",
        " lead",
        "trail ",
    ]
    if depth >= 4 or rng.random() < 0.4:
        if rng.random() < 0.5:
            return _random_tree(rng, 4)
        return " ".join(rng.choice(words) for _ in range(rng.randrange(1, 30)))
    if rng.random() < 0.5:
        return [_random_emit_tree(rng, depth + 1) for _ in range(rng.randrange(4))]
    return {
        rng.choice(["k", "key " * rng.randrange(1, 40), "\u00fc", "p" * rng.randrange(100, 140)])
        + str(i): _random_emit_tree(rng, depth + 1)
        for i in range(rng.randrange(4))
    }


@pytest.mark.parametrize("seed", range(300))
def test_a_fuzzed_document_emits_the_same_bytes_from_libyaml(seed: int) -> None:
    data = {"root": _random_emit_tree(random.Random(seed))}  # noqa: S311 -- seeded test data
    assert yaml_io.machine_yaml_to_str(data) == yaml_io.yaml_to_str(data)


def test_a_typical_lockfile_takes_the_libyaml_emitter() -> None:
    texts = [path.read_text(encoding="utf-8") for path in _LOCKFILES]
    agnostic = [yaml_io._emitter_agnostic(yaml_io.load_yaml_str(text)) for text in texts]
    assert sum(agnostic) >= len(agnostic) // 2


@pytest.mark.parametrize("length", range(118, 132))
def test_a_long_key_emits_the_same_bytes(length: int) -> None:
    key = "deployed/" + "k" * (length - 9)
    for data in ({key: "v"}, {"root": {key: "x" * 90}}, {"rows": [{key: 1}]}):
        assert yaml_io.machine_yaml_to_str(data) == yaml_io.yaml_to_str(data)


def test_a_long_tab_quoted_value_keeps_the_python_emitter() -> None:
    data = {"mcp": {"args": ["\t" + "}} b] " * 30]}}
    assert yaml_io._emitter_agnostic(data) is False
    assert yaml_io.machine_yaml_to_str(data) == yaml_io.yaml_to_str(data)


_GENERATOR_CASES = {
    "absent": "lockfile_version: '1'\ndependencies: []\n",
    "one": "apm_version: 0.38.0\nlockfile_version: '1'\n",
    "duplicate": "apm_version: 0.38.0\nlockfile_version: '1'\napm_version: 9.9.9\n",
    "flow": "{apm_version: 0.38.0, lockfile_version: '1', apm_version: '1.0'}\n",
    "null": "apm_version: null\n",
    "non-scalar": "apm_version: [1, 2]\n",
    "merge-key": "base: &b {apm_version: 1}\n<<: *b\n",
    "not-a-mapping": "- apm_version: 1\n",
    "empty": "",
    "malformed": "apm_version: [unclosed\n",
    "tab-indent": "apm_version:\t0.38.0\n",
    "bom-unicode": "\ufeffapm_version: '\u00fc'\n",
    "alias-value": "v: &v 0.38.0\napm_version: *v\n",
    "alias-key": "k: &k apm_version\n*k : 0.38.0\n",
    "huge-plain-scalar": "apm_version: " + "x" * 200_000 + "\n",
    "crlf": "apm_version: 0.38.0\r\nlockfile_version: '1'\r\n",
    "nul": "apm_version: 0.38.0\x00\n",
    "directive": "%YAML 1.1\n---\napm_version: 0.38.0\n",
    "two-documents": "apm_version: 1\n---\napm_version: 2\n",
    "question-in-flow": "apm_version: 0.38.0\nx: [a?b]\n",
    **BOMBS,
}


def _pure_top_level(
    text: str, loader: type = yaml_io._BoundedSafeLoader
) -> list[str | None] | None:
    try:
        node = yaml.compose(text, Loader=loader)
    except (yaml.YAMLError, ValueError, RecursionError):
        return None
    if not isinstance(node, yaml.MappingNode):
        return []
    values: list[str | None] = []
    for key_node, value_node in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            return None
        if isinstance(key_node, yaml.ScalarNode) and key_node.value == "apm_version":
            scalar = isinstance(value_node, yaml.ScalarNode)
            null = scalar and value_node.tag == "tag:yaml.org,2002:null"
            values.append(value_node.value if scalar and not null else None)
    return values


@pytest.mark.parametrize(
    "text",
    [*_GENERATOR_CASES.values(), *(p.read_text(encoding="utf-8") for p in _LOCKFILES)],
    ids=[*_GENERATOR_CASES, *(str(p.relative_to(ROOT)) for p in _LOCKFILES)],
)
def test_the_generator_check_reads_what_the_pure_parser_or_the_reader_reads(text: str) -> None:
    """Both scanners accept: identical. Only libyaml accepts: the reader's scan (#4939).

    Tab text is judged by the pure scanner only, as before.
    """
    got = yaml_io._top_level_scalar_values(text, "apm_version")
    pure = _pure_top_level(text)
    if pure is not None or "\t" in text:
        assert got == pure
    else:
        assert got == _pure_top_level(text, yaml_io._BoundedCSafeLoader)


@pytest.mark.skipif(
    yaml_io._BoundedCSafeLoader is yaml_io._BoundedSafeLoader, reason="PyYAML without libyaml"
)
def test_an_admitted_lockfile_is_composed_without_the_pure_scanner() -> None:
    from tests.unit.deps.test_lockfile_read_cost import _count_calls

    text = _LOCKFILES[0].read_text(encoding="utf-8")
    scans = _count_calls(
        yaml.scanner.Scanner.fetch_more_tokens.__code__,
        lambda: yaml_io._top_level_scalar_values(text, "apm_version"),
    )
    assert scans == 0
