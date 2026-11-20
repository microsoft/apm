"""The documented CLI surface matches the shipped Click tree (#4113).

Direction: Click tree -> docs. Every visible command and subcommand of
``apm`` needs a reference page (``docs/src/content/docs/reference/cli/
<command>.md``) that names it, and an entry in the apm-usage commands guide.
Every visible long option must appear in its command's docs: the whole page
for a top-level command; for a subcommand, the section whose heading names
``apm <path>`` plus the page's level-2 options sections (a subcommand with no
section of its own is checked against the whole page). Every visible command
and option must carry non-empty Click help, the ``--help`` half of the
surface.

Reverse direction: every ``apm ...`` command line in a code span or fenced
block of those docs, and every flag in the first column of a reference page
table, must still name a real command and real flags, so a rename or removal
without a docs update fails here. Prose placeholders (``NAME``, ``[OPTIONS]``,
``<pkg>``, ``{a|b}``) and quoted option values are skipped. Help-text
literals in ``src/`` are covered by the command-truth test, not here.

Hidden commands and options (``hidden=True`` in Click) are not part of the
documented surface: they are never required in the docs, and docs that still
mention them resolve (Click accepts them). Experimental features that ship as
visible commands or flags are documented like any other.

Known gaps are strict expected failures in ``cli_docs_surface_expected.json``
naming their tracking issue: an unlisted gap fails, and a listed gap that no
longer occurs fails until its entry is removed.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shlex
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

import click
import pytest

from tests.utils.click_tree import iter_commands, resolve, root_group

RATCHET_TEST_SCOPE = "repository"

pytestmark = [pytest.mark.component, pytest.mark.promise("P-DOC-cli-surface")]

REPO_ROOT = Path(__file__).resolve().parents[2]
REFERENCE_DIR = REPO_ROOT / "docs" / "src" / "content" / "docs" / "reference" / "cli"
GUIDE = REPO_ROOT / "packages" / "apm-guide" / ".apm" / "skills" / "apm-usage" / "commands.md"
GUIDE_NAME = "apm-usage/commands.md"
EXPECTED = Path(__file__).with_name("cli_docs_surface_expected.json")

KINDS = frozenset(
    {
        "command-not-in-guide",
        "command-not-in-page",
        "command-without-help",
        "command-without-page",
        "option-not-in-page",
        "option-without-help",
        "page-without-command",
        "unknown-command",
        "unknown-option",
        "unknown-table-flag",
    }
)

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_HEADING_PATH = re.compile(r"`apm ([a-z][a-z0-9-]*(?: [a-z][a-z0-9-]*)*)")
_FENCE = re.compile(r"^\s*(```|~~~)")
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_FLAG = re.compile(r"(?<![\w-])--\w[\w-]*")
_APM_PHRASE = re.compile(r"(?<![\w-])apm(?: \w[\w-]*)*")
_SHELL_BREAK = re.compile(r"\s(?:\|\||&&|\||;|2>|>|<|#)(?:\s|$)")
_PLACEHOLDER = re.compile(r"^(?:[\[<{(].*|[A-Z][A-Z0-9_:.-]*\]?(?:\.\.\.)?|\.\.\.)$")
_TRACKING = re.compile(r"^#[1-9][0-9]*$")


@dataclass(frozen=True, order=True)
class Mismatch:
    """One difference between the Click tree and the docs."""

    path: str
    kind: str
    detail: str


@dataclass(frozen=True)
class Docs:
    """Reference pages by top-level command name, plus the commands guide."""

    pages: Mapping[str, str]
    guide: str


@dataclass(frozen=True)
class _Section:
    title: str
    level: int
    start: int
    end: int


def load_docs(reference_dir: Path = REFERENCE_DIR, guide: Path = GUIDE) -> Docs:
    """Read every reference CLI page and the commands guide."""
    pages = {
        page.stem: page.read_text(encoding="utf-8") for page in sorted(reference_dir.glob("*.md"))
    }
    return Docs(pages=pages, guide=guide.read_text(encoding="utf-8"))


@lru_cache(maxsize=256)
def _mentioned(text: str) -> frozenset[str]:
    """Every ``--flag`` and every ``apm word...`` prefix written in *text*."""
    found = set(_FLAG.findall(text))
    for match in _APM_PHRASE.findall(text):
        words = match.split()
        found.update(" ".join(words[:end]) for end in range(1, len(words) + 1))
    return frozenset(found)


def _mentions(text: str, phrase: str) -> bool:
    """Whether *phrase* (a flag or an ``apm ...`` command) appears as whole words."""
    return phrase in _mentioned(text)


def _options(command: click.Command) -> list[click.Option]:
    """Return every option Click parses for *command*, including generated ones."""
    with click.Context(command, info_name=command.name) as ctx:
        return [param for param in command.get_params(ctx) if isinstance(param, click.Option)]


def _long_flags(options: Iterable[click.Option]) -> list[str]:
    return [f for o in options for f in (*o.opts, *o.secondary_opts) if f.startswith("--")]


def _visible_commands(group: click.Group) -> dict[tuple[str, ...], click.Command]:
    """Every command not hidden itself or below a hidden group."""
    hidden: set[tuple[str, ...]] = set()
    visible: dict[tuple[str, ...], click.Command] = {}
    for path, command in iter_commands(group):
        if command.hidden or any(path[:depth] in hidden for depth in range(1, len(path))):
            hidden.add(path)
        else:
            visible[path] = command
    return visible


def _sections(lines: list[str]) -> list[_Section]:
    """Return heading spans (heading line to the next same-or-higher heading)."""
    heads: list[tuple[int, int, str]] = []
    in_fence = False
    for index, line in enumerate(lines):
        if _FENCE.match(line):
            in_fence = not in_fence
        elif not in_fence and (match := _HEADING.match(line)):
            heads.append((index, len(match.group(1)), match.group(2)))
    sections = []
    for position, (start, level, title) in enumerate(heads):
        ends = [line for line, lvl, _ in heads[position + 1 :] if lvl <= level]
        sections.append(_Section(title, level, start, ends[0] if ends else len(lines)))
    return sections


def _heading_path(title: str) -> tuple[str, ...] | None:
    match = _HEADING_PATH.search(title)
    return tuple(match.group(1).split()) if match else None


@lru_cache(maxsize=256)
def _option_scope(page: str, path: tuple[str, ...]) -> str:
    """Docs text in which the options of the command at *path* must appear."""
    if len(path) == 1:
        return page
    lines = page.splitlines()
    sections = _sections(lines)
    own = [s for s in sections if _heading_path(s.title) == path]
    if not own:
        return page
    shared = [s for s in sections if s.level == 2 and "option" in s.title.lower()]
    return "\n".join("\n".join(lines[s.start : s.end]) for s in (*own, *shared))


def _code_spans(text: str) -> Iterator[str]:
    """Yield fenced-block lines and inline code spans."""
    in_fence = False
    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
        elif in_fence:
            yield line.strip()
        else:
            yield from _INLINE_CODE.findall(line)


def _apm_command_lines(text: str) -> Iterator[str]:
    for code in _code_spans(text):
        code = code.removeprefix("$ ").strip()
        if code.startswith("apm "):
            yield _SHELL_BREAK.split(code, maxsplit=1)[0].strip()


def _argv(line: str) -> list[str] | None:
    """Split one documented command line; drop placeholders and quoted values."""
    try:
        tokens = shlex.split(line)
    except ValueError:
        return None
    if "--" in tokens:
        tokens = tokens[: tokens.index("--")]
    return [t for t in tokens[1:] if not _PLACEHOLDER.match(t) and not re.search(r"\s", t)]


def _flag_rows(text: str) -> Iterator[tuple[tuple[str, ...] | None, str]]:
    """Yield ``(section command path, flag)`` for each flag-table row."""
    lines = text.splitlines()
    owned = [(s, _heading_path(s.title)) for s in _sections(lines)]
    for index, line in enumerate(lines):
        cells = line.split("|")
        if not line.startswith("|") or len(cells) < 3:
            continue
        codes = _INLINE_CODE.findall(cells[1])
        if not codes or not all(code.startswith("-") for code in codes):
            continue
        owners = [path for s, path in owned if path and s.start <= index < s.end]
        for flag in _FLAG.findall(" ".join(codes)):
            yield (owners[-1] if owners else None), flag


@dataclass(frozen=True)
class _Tree:
    """One snapshot of the Click tree: commands and parsed options by path."""

    group: click.Group
    commands: Mapping[tuple[str, ...], click.Command]
    options: Mapping[tuple[str, ...], list[click.Option]]

    @classmethod
    def build(cls, group: click.Group) -> _Tree:
        commands = dict(iter_commands(group))
        options = {path: _options(command) for path, command in {(): group, **commands}.items()}
        return cls(group, commands, options)

    def subtree_flags(self, path: tuple[str, ...]) -> frozenset[str]:
        return frozenset(
            _long_flags(
                o for p, opts in self.options.items() if p[: len(path)] == path for o in opts
            )
        )


def _page_checks(docs: Docs, tree: _Tree) -> Iterator[Mismatch]:
    for name in docs.pages:
        if (name,) not in tree.commands:
            yield Mismatch(name, "page-without-command", f"reference/cli/{name}.md")


def _command_checks(docs: Docs, tree: _Tree) -> Iterator[Mismatch]:
    for path, command in _visible_commands(tree.group).items():
        key = " ".join(path)
        phrase = f"apm {key}"
        if not (command.help or command.short_help or "").strip():
            yield Mismatch(key, "command-without-help", phrase)
        options = [o for o in tree.options[path] if not o.hidden and "--help" not in o.opts]
        for option in options:
            if not (option.help or "").strip():
                yield Mismatch(key, "option-without-help", option.opts[0])
        if not _mentions(docs.guide, phrase):
            yield Mismatch(key, "command-not-in-guide", phrase)
        page = docs.pages.get(path[0])
        if page is None:
            yield Mismatch(key, "command-without-page", f"reference/cli/{path[0]}.md")
            continue
        if len(path) > 1 and not _mentions(page, phrase):
            yield Mismatch(key, "command-not-in-page", phrase)
        scope = _option_scope(page, path)
        for flag in _long_flags(options):
            if not _mentions(scope, flag):
                yield Mismatch(key, "option-not-in-page", flag)


def _line_mismatches(line: str, tree: _Tree) -> list[tuple[str, str]]:
    """Return ``(kind, detail)`` for one documented ``apm ...`` command line."""
    argv = _argv(line)
    if argv is None:
        return []
    result = resolve(argv, tree.group)
    if result.unknown_command:
        return [("unknown-command", line)]
    known = {f for o in tree.options[result.path] for f in (*o.opts, *o.secondary_opts)}
    return [
        ("unknown-option", f"{line} :: {flag}")
        for flag in result.unknown_options
        if flag.startswith("--") and flag.split("=", 1)[0] not in known
    ]


def _command_line_checks(docs: Docs, tree: _Tree) -> Iterator[Mismatch]:
    sources = {f"reference/cli/{n}.md": t for n, t in docs.pages.items()}
    sources[GUIDE_NAME] = docs.guide
    verdicts: dict[str, list[tuple[str, str]]] = {}
    for source, text in sources.items():
        for line in dict.fromkeys(_apm_command_lines(text)):
            if line not in verdicts:
                verdicts[line] = _line_mismatches(line, tree)
            for kind, detail in verdicts[line]:
                yield Mismatch(source, kind, detail)


def _flag_table_checks(docs: Docs, tree: _Tree) -> Iterator[Mismatch]:
    for name, text in docs.pages.items():
        if (name,) not in tree.commands:
            continue
        for owner, flag in _flag_rows(text):
            path = owner if owner in tree.commands else (name,)
            if flag not in tree.subtree_flags(path):
                detail = f"apm {' '.join(path)} :: {flag}"
                yield Mismatch(f"reference/cli/{name}.md", "unknown-table-flag", detail)


def surface_mismatches(docs: Docs, group: click.Group | None = None) -> list[Mismatch]:
    """Compare the Click tree with the reference pages and the commands guide."""
    tree = _Tree.build(group or root_group())
    checks = (_page_checks, _command_checks, _command_line_checks, _flag_table_checks)
    return sorted({mismatch for check in checks for mismatch in check(docs, tree)})


def load_expected(path: Path = EXPECTED) -> dict[Mismatch, str]:
    """Return ``{mismatch: tracking issue}`` from the expected-failure list."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        Mismatch(entry["path"], entry["kind"], entry["detail"]): entry["tracking"]
        for entry in payload["expected_failures"]
    }


def ratchet_failures(found: Iterable[Mismatch], expected: Mapping[Mismatch, str]) -> list[str]:
    """Strict expected failures: report unlisted gaps and listed gaps now fixed."""
    found = set(found)
    failures = [
        f"[x] {m.path}: {m.kind}: {m.detail}. Fix: document it (or rename the docs), "
        f"or list it in {EXPECTED.name} with its tracking issue."
        for m in sorted(found - expected.keys())
    ]
    failures += [
        f"[x] {m.path}: {m.kind}: {m.detail} ({expected[m]}) is now fixed; remove the entry "
        f"from {EXPECTED.name}."
        for m in sorted(expected.keys() - found)
    ]
    return failures


@pytest.fixture(scope="module")
def docs() -> Docs:
    return load_docs()


def test_cli_surface_matches_reference_docs_and_guide(docs: Docs) -> None:
    failures = ratchet_failures(surface_mismatches(docs), load_expected())
    assert not failures, "\n".join(failures)


def test_expected_failures_are_well_formed() -> None:
    entries = json.loads(EXPECTED.read_text(encoding="utf-8"))["expected_failures"]
    keys = [(e["path"], e["kind"], e["detail"]) for e in entries]
    assert keys == sorted(set(keys)), "entries must be unique and sorted by path, kind, detail"
    for entry in entries:
        assert set(entry) == {"path", "kind", "detail", "tracking"}, entry
        assert entry["kind"] in KINDS, entry
        assert _TRACKING.match(entry["tracking"]), entry


def test_ratchet_rejects_unlisted_and_fixed_entries() -> None:
    gap = Mismatch("install", "option-not-in-page", "--probe")
    fixed = Mismatch("deps", "option-not-in-page", "--gone")
    failures = ratchet_failures([gap], {fixed: "#1"})
    assert failures == [
        "[x] install: option-not-in-page: --probe. Fix: document it (or rename the docs), "
        "or list it in cli_docs_surface_expected.json with its tracking issue.",
        "[x] deps: option-not-in-page: --gone (#1) is now fixed; remove the entry from "
        "cli_docs_surface_expected.json.",
    ]


def _command(path: str) -> click.Command:
    result = resolve(path.split())
    assert result.ok and result.command is not None
    return result.command


def _without_option(command: click.Command, flag: str) -> list[click.Parameter]:
    params = [p for p in command.params if flag not in getattr(p, "opts", ())]
    assert len(params) == len(command.params) - 1
    return params


def test_added_option_without_docs_is_reported(docs: Docs, monkeypatch: pytest.MonkeyPatch) -> None:
    install = _command("install")
    probe = click.Option(["--surface-probe"], is_flag=True, help="Probe.")
    monkeypatch.setattr(install, "params", [*install.params, probe])
    assert Mismatch("install", "option-not-in-page", "--surface-probe") in surface_mismatches(docs)


def test_added_subcommand_without_docs_is_reported(
    docs: Docs, monkeypatch: pytest.MonkeyPatch
) -> None:
    deps = _command("deps")
    assert isinstance(deps, click.Group)
    monkeypatch.setitem(deps.commands, "probe", click.Command("probe", help="Probe."))
    found = surface_mismatches(docs)
    assert Mismatch("deps probe", "command-not-in-page", "apm deps probe") in found
    assert Mismatch("deps probe", "command-not-in-guide", "apm deps probe") in found


def test_hidden_command_and_option_are_not_required(
    docs: Docs, monkeypatch: pytest.MonkeyPatch
) -> None:
    deps = _command("deps")
    assert isinstance(deps, click.Group)
    hidden = click.Command("probe", help="Probe.", hidden=True)
    monkeypatch.setitem(deps.commands, "probe", hidden)
    install = _command("install")
    probe = click.Option(["--surface-probe"], is_flag=True, hidden=True)
    monkeypatch.setattr(install, "params", [*install.params, probe])
    found = surface_mismatches(docs)
    assert not [m for m in found if "probe" in m.path or "probe" in m.detail]


def test_removed_doc_mention_is_reported(docs: Docs) -> None:
    pages = dict(docs.pages)
    pages["install"] = pages["install"].replace("--frozen", "--fr0zen")
    found = surface_mismatches(replace(docs, pages=pages))
    assert Mismatch("install", "option-not-in-page", "--frozen") in found


def test_renamed_option_is_reported_in_both_directions(
    docs: Docs, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _command("install")
    renamed = click.Option(["--locked"], is_flag=True, help="Renamed --frozen.")
    monkeypatch.setattr(install, "params", [*_without_option(install, "--frozen"), renamed])
    found = surface_mismatches(docs)
    assert Mismatch("install", "option-not-in-page", "--locked") in found
    assert (
        Mismatch("reference/cli/install.md", "unknown-table-flag", "apm install :: --frozen")
        in found
    )
    assert any(m.kind == "unknown-option" and m.detail.endswith(":: --frozen") for m in found), (
        "a documented 'apm install --frozen' line must no longer resolve"
    )


def test_renamed_command_is_reported_in_both_directions(
    docs: Docs, monkeypatch: pytest.MonkeyPatch
) -> None:
    deps = _command("deps")
    assert isinstance(deps, click.Group)
    monkeypatch.setitem(deps.commands, "graph", deps.commands["tree"])
    monkeypatch.delitem(deps.commands, "tree")
    found = surface_mismatches(docs)
    assert Mismatch("deps graph", "command-not-in-page", "apm deps graph") in found
    assert any(m.kind == "unknown-command" and m.detail.startswith("apm deps tree") for m in found)


def test_page_without_command_is_reported(docs: Docs) -> None:
    pages = {**docs.pages, "probe": "# apm probe\n"}
    found = surface_mismatches(replace(docs, pages=pages))
    assert Mismatch("probe", "page-without-command", "reference/cli/probe.md") in found


_RENAMED_FLAG_MUTANT = (
    REPO_ROOT / "tests" / "fixtures" / "cli_surface_mutants" / "renamed_frozen_flag.py"
)


def test_ratchet_kills_the_recorded_renamed_flag_mutant(
    docs: Docs, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = importlib.util.spec_from_file_location("renamed_frozen_flag", _RENAMED_FLAG_MUTANT)
    assert spec is not None and spec.loader is not None
    mutant = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mutant)
    assert not ratchet_failures(surface_mismatches(docs), load_expected())

    install = _command("install")
    monkeypatch.setattr(install, "params", mutant.mutant_params(install))
    failures = ratchet_failures(surface_mismatches(docs), load_expected())

    assert any("option-not-in-page: --locked" in f for f in failures), failures
    assert any("unknown-table-flag: apm install :: --frozen" in f for f in failures), failures
