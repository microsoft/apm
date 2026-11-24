"""Docs journeys through the real CLI: tests/lifecycle/journeys (#4110).

Each journey file replays one user journey from the docs in a fresh hermetic
lifecycle world. ``index.yaml`` is the catalogue: it pins the ids, the step
numbers and the oracles (O1-O13) each step must pass. This module runs every
``{consumer,producer,enterprise}/*.yaml`` file against it.

Journey YAML schema (``schema: 1``)
-----------------------------------
::

    schema: 1
    id: J01                      # an index.yaml journey id; the file sits in <persona>/
    persona: consumer            # consumer | producer | enterprise
    title: First run (quickstart)
    docs: [quickstart.mdx]       # pages under docs/src/content/docs/ (the index's list)
    world:                       # starting state, all keys optional
      from: J01                  # replay that journey's steps first (exit codes checked only)
      project: empty             # keep (default) | empty: drop the world's apm.yml
      git: init                  # none (default) | init: git init the project
      policy: allow              # org policy variant (world.scopes.use_policy)
      setup: [<line>]            # command lines run before step 1; each must exit 0
      env: {K: V}                # added to the world env for the whole journey
    allow_repeat: []             # O4: lines allowed to print twice within one command
    steps:                       # exactly the index's steps, numbered 1..N
      - n: 1
        run: [apm --version]     # command lines in order; "apm ..." runs through
                                 # driver.run, anything else as a plain process (no
                                 # shell); "{release}" is the current release tag
        exit: 0                  # exit code of every command, or one per command
        scope: project           # project (default) | user: apm lines get the scope flag
        env: {K: V}              # env for this step's commands only
        edit:                    # applied before run (index kind "edit")
          - {append: <path>, text: <text>}     # creates a missing file
          - {replace: <path>, old: <text>, new: <text>}
          - {serve: <owner/repo>, path: <file>, text: <text>}  # commit upstream
          - {unreachable: <owner/repo>}  # that repo answers 503 ...
          - {reachable: <owner/repo>}    # ... until this edit or the journey's end
        cold_cache: true         # empty APM_CACHE_DIR before run (a fresh machine)
        chdir: ../teammate       # after run the project is this directory ("cd")
        converged: true          # I2: the step starts converged, so it is a byte no-op
        drift: true              # a user edit of a deployed file is pending: I1 n/a
        git_clean: true          # O9: "git status --porcelain" is empty after the step
        files: [apm.yml, ".github/prompts/*", apm_modules/]
                                 # O9: project paths the step creates, changes or
                                 # removes (fnmatch; "apm_modules/" covers its tree)
        key_lines: ["[+] ..."]   # must print (substring of the normalized output)
        file_lines: {apm.sarif: ['"level": "error"']}  # KEY: substrings of project files
        forbidden_lines: ["[!]"] # must not print
        docs:                    # O13 / O2 source of the step
          page: quickstart.mdx   # or /README.md (repository root)
          fence: <title>         # optional: the fence with this title= attribute
          commands: [<line>]     # literal lines of one fence; each must be in run
          output: [<line>]       # O2: consecutive lines of one text fence ("..." ok)
          pins: true             # O13: versions pinned in the fence == current release
        help_docs: reference/cli/install.md  # O7: --help flags == the page's option flags
        remediate:               # O8a on a red step: follow the printed advice
          - {delete: <regex>}    # delete each project path the regex (group 1) lists
          - {advice: apm install}  # must print as 'apm install'; then it runs
        rerun: [apm install --frozen]  # O8a: must exit 0 after remediate (default:
                                 # the step's failing apm commands)
        diagnose:                # O8b on a red step: printed DIAGNOSTIC advice
          advice: apm install --verbose <dep>  # must print as '<advice>'; then it runs
          promises: [<line>]     # lines the advice prints that the step did not
        remediate_from: 4        # apply step 4's remediate to its output, in place
    known_cells:                 # STRICT expected failures on main
      - id: <slug>
        law: FORBID              # I1-I9, O1-O13, KEY (key line missing), FORBID
        tracking: "#0"           # issue; "#0" until the integrator files it
        step: 4                  # the step number it fails on
        match: <regex>           # matched against the violation message

Oracles: a step runs the oracles index.yaml assigns to it; O3, O4 and O5 run
on every apm command, O13 on every step with ``docs.commands`` and KEY/FORBID
on every step. ``check_cross_cutting`` (I1-I9) runs after every apm command.
O10 (twin fidelity) needs the real upstream: a step that names it announces a
``JourneyDeferredOracle`` warning and the ``live`` node
``test_twin_fidelity_o10`` checks it (nightly, ``-m live``). O11 has no
hermetic implementation yet and fails loudly where the index assigns it.

Tiers: golden journeys (index ``golden: true``) carry ``lifecycle_smoke`` (PR);
the rest run nightly: ``pytest tests/lifecycle/journeys -m "not
lifecycle_smoke"``. Goldens: ``goldens/<J>/<nn>.txt``, written by
``regen_goldens.py``.
"""

from __future__ import annotations

import contextlib
import difflib
import fnmatch
import importlib
import json
import os
import re
import shlex
import shutil
import subprocess
import warnings
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
import yaml

from apm_cli.deps.lockfile import LockFile
from tests.lifecycle import driver
from tests.lifecycle.journeys import normalize as norm
from tests.lifecycle.journeys.twins import RELEASE_TAG, TWINS, publish_twins
from tests.lifecycle.known import KnownFailure
from tests.lifecycle.oracles import (
    MUTATING_COMMANDS,
    ORACLE_LAWS,
    Evidence,
    ExpectedCell,
    Observation,
    check_cross_cutting,
    check_obligation,
    operation_for,
)
from tests.lifecycle.snapshot import LifecycleSurfaceRoots
from tests.lifecycle.world.clone import clone, remove_tree
from tests.lifecycle.world.scopes import use_policy
from tests.utils.apm_lifecycle_runner import CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshot, ArtifactSnapshotSet
from tests.utils.isolated_apm_environment import DURABLE_ENVIRONMENT_ROOTS
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_scripted_https_proxy import ScriptedResponse

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.promise(
        "P-INV-I1",
        "P-INV-I2",
        "P-INV-I3",
        "P-INV-I4",
        "P-INV-I5",
        "P-INV-I6",
        "P-INV-I7",
        "P-INV-I8",
        "P-INV-I9",
    ),
]

HERE = Path(__file__).parent
REPO = HERE.parents[2]
DOCS = REPO / "docs" / "src" / "content" / "docs"
INDEX = HERE / "index.yaml"
PERSONAS = ("consumer", "producer", "enterprise")
RECORD_ENV = "APM_JOURNEY_RECORD_DIR"
SCHEMA = 1
_JOURNEY_KEYS = {"schema", "id", "persona", "title", "docs", "steps"}
_STEP_KEYS = {
    "n", "run", "exit", "edit", "cold_cache", "chdir", "converged", "drift", "git_clean",
    "files", "key_lines", "forbidden_lines", "docs", "help_docs", "remediate", "rerun",
    "remediate_from", "note", "scope", "env", "file_lines", "diagnose",
}  # fmt: skip
# O8 is split (#4571): O8a remediation advice, O8b diagnostic advice.
_O_IDS = frozenset({*(f"O{i}" for i in range(1, 14) if i != 8), "O8a", "O8b"})
_O_LAWS = _O_IDS | {"KEY", "FORBID"}
_ADVICE_LAWS = frozenset({"O8a", "O8b"})
# HOME paths a project command may write: receipts and bookkeeping (as in rows.py).
_HOME_ALLOWED = (
    ".apm/deployment-receipts",
    ".apm-local",
    ".local/state/gh",
    ".cache/apm",
    # Claude Code's marketplace registry and APM's record of its entry (#5176).
    ".claude/plugins/known_marketplaces.json",
    ".apm/claude-known-marketplaces.json",
)
# $APM_HOME entries that user-configuration commands own (declared write set for I5).
_APM_HOME_WRITES: Mapping[str, tuple[str, ...]] = {
    "marketplace": ("marketplaces.json", "marketplaces.lock", "cache"),
    "lifecycle": (
        "scripts-trust.json",
        "scripts-trust.json.lock",
        "logs/scripts.log",
        "logs/scripts.log.lock",
    ),
    "experimental": ("config.json",),
    "config": ("config.json",),
    # A trusted project lifecycle script run appends its log (reference/cli/lifecycle.md).
    "install": ("logs/scripts.log", "logs/scripts.log.lock"),
}
# Written only by a successful command: a marketplace install records its version pin,
# and an applied update refreshes the registered marketplace's metadata cache. A
# --dry-run writes neither (#4484).
_APM_HOME_WRITES_ON_SUCCESS: Mapping[str, tuple[str, ...]] = {
    "install": ("cache/marketplace/version-pins.json",),
    "update": ("cache/marketplace/lifecycle-market.meta.json",),
}
_UNTRACKED_ROOTS = frozenset({".git", ".apm-local"})
_DECLARED_WRITES = ("apm.yml", "apm.lock.yaml", ".gitignore", "apm_modules/")
_VERSION_PIN = re.compile(
    r"apm-version:\s*['\"]?(\d+\.\d+\.\d+)|apm-cli==(\d+\.\d+\.\d+)|microsoft/apm/\S*#v(\d+\.\d+\.\d+)"
)
_FENCE = re.compile(
    r"^(?P<indent>[ \t]*)```(?P<info>[^\n]*)\n(?P<body>.*?)^(?P=indent)```[ \t]*$", re.M | re.S
)


class JourneyDeferredOracle(UserWarning):
    """An oracle that needs the real network ran nowhere in this session (O10)."""


# -- loading -----------------------------------------------------------------


def load_index() -> dict:
    return yaml.safe_load(INDEX.read_text(encoding="ascii"))


def journey_files() -> list[Path]:
    return sorted(path for persona in PERSONAS for path in (HERE / persona).glob("*.yaml"))


def load_journey(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="ascii"))


def journey_by_id(journey_id: str) -> dict:
    for path in journey_files():
        data = load_journey(path)
        if data["id"] == journey_id:
            return data
    raise KeyError(f"no journey file for {journey_id}")


def check_journey_file(path: Path, index: dict) -> list[str]:
    """Schema problems of one journey file against the index (empty when it is sound)."""
    data = load_journey(path)
    jid = data.get("id")
    entry = next((j for j in index["journeys"] if j["id"] == jid), None)
    problems = [f"{path.name}: missing {key}" for key in sorted(_JOURNEY_KEYS - set(data))]
    if data.get("schema") != SCHEMA:
        problems.append(f"{jid}: schema must be {SCHEMA}")
    if entry is None:
        return [*problems, f"{path.name}: id {jid!r} is not in index.yaml"]
    if path.parent.name != entry["persona"] or data.get("persona") != entry["persona"]:
        problems.append(f"{jid}: persona must be {entry['persona']} (and its directory)")
    numbers = [step.get("n") for step in data.get("steps") or []]
    if numbers != [step["n"] for step in entry["steps"]]:
        problems.append(f"{jid}: steps {numbers} differ from the index's {len(entry['steps'])}")
    oracles = {spec["n"]: set(spec.get("oracles") or ()) for spec in entry["steps"]}
    for step in data.get("steps") or []:
        unknown = set(step) - _STEP_KEYS
        if unknown:
            problems.append(f"{jid} step {step.get('n')}: unknown keys {sorted(unknown)}")
        problems += [f"{jid} step {step.get('n')}: {p}" for p in _advice_problems(step, oracles)]
    for cell in data.get("known_cells") or []:
        if not re.fullmatch(r"#\d+", str(cell.get("tracking"))):
            problems.append(f"{jid}: known cell {cell.get('id')} needs tracking '#<n>'")
        if cell.get("law") not in set(ORACLE_LAWS) | _O_LAWS:
            problems.append(f"{jid}: known cell {cell.get('id')} has unknown law")
        if cell.get("step") not in numbers:
            problems.append(f"{jid}: known cell {cell.get('id')} names no step")
    return problems


def _advice_problems(step: dict, oracles: Mapping[int, set[str]]) -> list[str]:
    """Each advice line is either remediation (O8a, ``remediate``) or diagnostic (O8b,
    ``diagnose`` with the information it promises), as the index marks the step."""
    laws = oracles.get(step.get("n"), set())
    diagnose = step.get("diagnose")
    problems = []
    if step.get("remediate") and diagnose:
        problems.append("advice is remediation or diagnostic, not both")
    if "O8b" in laws and not (diagnose and diagnose.get("advice") and diagnose.get("promises")):
        problems.append("O8b needs diagnose: {advice, promises}")
    if diagnose and "O8b" not in laws:
        problems.append("diagnose advice needs O8b in the index")
    if step.get("remediate") and "O8a" not in laws and not step.get("remediate_from"):
        problems.append("remediate advice needs O8a in the index")
    return problems


# -- docs --------------------------------------------------------------------


@dataclass(frozen=True)
class Fence:
    info: str
    lines: tuple[str, ...]

    @property
    def title(self) -> str:
        match = re.search(r'title="([^"]+)"', self.info)
        return match.group(1) if match else ""


def page_fences(page: str) -> list[Fence]:
    path = REPO / page.lstrip("/") if page.startswith("/") else DOCS / page
    text = path.read_text(encoding="utf-8")
    fences = []
    for match in _FENCE.finditer(text):
        width = len(match.group("indent"))
        lines = tuple(line[width:] for line in match.group("body").split("\n")[:-1])
        fences.append(Fence(match.group("info").strip(), lines))
    return fences


def _stripped(line: str) -> str:
    """A fence line as a command: no list dash or ``run:`` key, no trailing ``# comment``."""
    line = re.sub(r"^(?:-\s+)?(?:run:\s+)?", "", line.strip())
    return re.sub(r"\s+#\s.*$", "", line)


def docs_problems(docs: Mapping) -> list[str]:
    """O13 (static half): commands, output and pins are literally in the page."""
    page = docs["page"]
    fences = [f for f in page_fences(page) if not docs.get("fence") or f.title == docs["fence"]]
    problems: list[str] = []
    commands = [_sub(c) for c in docs.get("commands") or ()]
    holding = [f for f in fences if all(c in {_stripped(x) for x in f.lines} for c in commands)]
    if commands and not holding:
        problems.append(f"O13: {page}: no fence holds the commands {commands}")
    output = [str(line) for line in docs.get("output") or ()]
    if output and not any(_consecutive(output, list(f.lines)) for f in fences):
        problems.append(f"O13: {page}: the output block {output[:2]}... is not in the page")
    if docs.get("pins"):
        for fence in holding or fences:
            for match in _VERSION_PIN.finditer("\n".join(fence.lines)):
                pinned = next(group for group in match.groups() if group)
                if f"v{pinned}" != RELEASE_TAG:
                    problems.append(f"O13: {page} pins {pinned}, current release is {RELEASE_TAG}")
    return problems


def _consecutive(needle: list[str], hay: list[str]) -> bool:
    size = len(needle)
    return any(hay[i : i + size] == needle for i in range(len(hay) - size + 1))


def help_flags(text: str) -> set[str]:
    flags: set[str] = set()
    in_options = False
    for line in text.splitlines():
        if line.startswith("Options:"):
            in_options = True
        elif in_options and re.match(r"^\S", line):
            in_options = False
        elif in_options and re.match(r"^  (-\w, )?--", line):
            flags.update(re.findall(r"--[a-z][a-z-]*", re.split(r"\s{2,}", line.strip())[0]))
    return flags - {"--help"}


def docs_flags(page: str) -> set[str]:
    text = (DOCS / page).read_text(encoding="utf-8")
    section = text.split("\n## Options", 1)[-1].split("\n## ", 1)[0]
    flags: set[str] = set()
    for line in section.splitlines():
        if line.startswith("| `-"):
            flags.update(re.findall(r"--[a-z][a-z-]*", line.split("|")[1]))
    return flags - {"--help"}


# -- the run -----------------------------------------------------------------


@dataclass
class _Command:
    line: str
    result: CommandResult
    stdout: str  # normalized
    stderr: str


@dataclass
class _StepFacts:
    """What one step did: its commands, their expected exits and the project delta.

    ``evidence`` collects, per oracle, the facts it checked: ``check_obligation``
    re-verifies them when the step's obligation ``J|<J>|<n>|<O>`` is credited.
    """

    step: dict
    commands: list[_Command]
    exits: list[tuple[_Command, int]]
    before: ArtifactSnapshotSet
    after: ArtifactSnapshotSet
    evidence: dict[str, tuple[Evidence, CommandResult]] = field(default_factory=dict)

    @property
    def delta(self) -> set[str]:
        return _delta(self.before.snapshot("project"), self.after.snapshot("project"))

    @property
    def last(self) -> CommandResult | None:
        """The step's last apm command (its last command when it runs no apm; None if none)."""
        commands = self.apm or self.commands
        return commands[-1].result if commands else None

    def combined(self, *, normalized: bool) -> CommandResult | None:
        """The last command's result carrying the whole step's (normalized) output."""
        pick = (
            (lambda c: (c.stdout, c.stderr))
            if normalized
            else (lambda c: (c.result.stdout, c.result.stderr))
        )
        last = self.last
        if last is None:
            return None
        return CommandResult(
            last.command,
            last.returncode,
            "\n".join(pick(c)[0] for c in self.commands),
            "\n".join(pick(c)[1] for c in self.commands),
            last.cwd,
        )

    def record(self, law: str, evidence: Evidence, result: CommandResult | None = None) -> None:
        self.evidence[law] = (evidence, result or self.combined(normalized=True))

    @property
    def apm(self) -> list[_Command]:
        return [c for c in self.commands if c.line.split()[0] == "apm"]

    @property
    def text(self) -> str:
        return "\n".join(f"{c.stdout}\n{c.stderr}" for c in self.commands)


@dataclass
class _JourneyModel:
    """The OracleModel of one journey step: no gates, so nothing is ever disallowed."""

    converged: bool
    cells: Mapping[str, tuple[ExpectedCell, ...]]
    seen: set[str]

    def allowed_surfaces(self) -> frozenset[str]:
        return frozenset()

    def converged_before(self) -> bool:
        return self.converged

    def expected_cells(self, op, before, after, result):
        del op, before, after, result
        return self.cells

    def record_expected(self, cell_id: str) -> None:
        self.seen.add(cell_id)


@dataclass
class JourneyRun:
    world: object
    journey: dict
    index_entry: dict
    record_dir: Path | None = None
    outputs: dict[int, list[_Command]] = field(default_factory=dict)
    seen: set[str] = field(default_factory=set)
    _experimental: tuple[str, ...] | None = None
    _pending: dict[int, list[tuple]] = field(default_factory=dict)

    # setup ------------------------------------------------------------------

    def start(self) -> None:
        self.world.env.update(norm.JOURNEY_ENV)
        self.world.env["APM_LIFECYCLE_JOURNEY"] = self.journey["id"]
        world = self.journey.get("world") or {}
        self.world.env.update({k: str(v) for k, v in (world.get("env") or {}).items()})
        if world.get("from"):
            self.replay(journey_by_id(world["from"]))
        if world.get("project") == "empty":
            (self.world.project / "apm.yml").unlink()
        if world.get("git") == "init":
            self._shell("git init -q --initial-branch=main")
        if world.get("policy"):
            use_policy(self.world, world["policy"])
        for line in world.get("setup") or ():
            # A failing setup line is a world fault; never let it surface later as a product step.
            cmd = self._execute(line)
            assert cmd.result.returncode == 0, (
                f"setup {line!r} -> {cmd.result.returncode}\n{cmd.stdout}\n{cmd.stderr}"
            )

    def replay(self, other: dict) -> None:
        """Run another journey's steps as setup: exit codes only, no oracles."""
        nested = JourneyRun(self.world, other, {}, None)
        nested.start()
        for step in other["steps"]:
            nested._prepare(step)
            commands = []
            for line, expected in zip(step["run"], _exits(step), strict=True):
                commands.append(nested._execute(line, scope=step.get("scope", "project")))
                code = commands[-1].result.returncode
                assert code == expected, f"setup {other['id']} step {step['n']}: {line!r} -> {code}"
            nested.outputs[step["n"]] = commands
            nested._finish(step)

    def _prepare(self, step: dict) -> None:
        if step.get("cold_cache"):
            cache = Path(self.world.env["APM_CACHE_DIR"])
            remove_tree(cache)
            cache.mkdir(parents=True)
        for edit in step.get("edit") or ():
            if "serve" in edit:
                self._serve(edit["serve"], edit["path"], _sub(edit["text"]), step["n"])
                continue
            if "unreachable" in edit:
                _unreachable(self.world.services).add(edit["unreachable"])
                continue
            if "reachable" in edit:
                _unreachable(self.world.services).discard(edit["reachable"])
                continue
            path = self.world.project / (edit.get("append") or edit["replace"])
            text = path.read_text(encoding="utf-8") if path.exists() or "replace" in edit else ""
            if "append" in edit:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text + _sub(edit["text"]), encoding="utf-8")
            else:
                old = _sub(edit["old"])
                assert old in text, f"edit: {old!r} not in {path.name}"
                path.write_text(text.replace(old, _sub(edit["new"]), 1), encoding="utf-8")
        if step.get("remediate_from"):
            source = step["remediate_from"]
            spec = next(s for s in self.journey["steps"] if s["n"] == source)
            self._remediate(self.world, spec["remediate"], self._text(source))

    def _serve(self, slug: str, relative: str, text: str, n: int) -> None:
        """Commit *text* as *relative* to the served repository *slug* (an upstream change)."""
        origin = self.world.services._repositories[slug]
        work = self.world.root / f"serve-{n}-{slug.replace('/', '__')}"
        git = ("git", "-C", str(work))
        steps = [
            ("git", "clone", "-q", str(origin), str(work)),
            (*git, "add", "--all"),
            (*git, "commit", "-q", "-m", f"journey step {n}: {relative}"),
            (*git, "push", "-q", "origin", "HEAD:main"),
        ]
        for index, command in enumerate(steps):
            if index == 1:
                (work / relative).parent.mkdir(parents=True, exist_ok=True)
                (work / relative).write_text(text, encoding="utf-8")
            subprocess.run(command, env=self.world.env, check=True, capture_output=True, timeout=60)
        remove_tree(work)

    def close(self) -> None:
        _unreachable(self.world.services).clear()

    def _finish(self, step: dict) -> None:
        if step.get("chdir"):
            self.world.project = (self.world.project / step["chdir"]).resolve()

    # commands ---------------------------------------------------------------

    def _paths(self) -> dict[str, Path | str]:
        world = self.world
        return {
            "<PROJECT>": world.project,
            "<APM_HOME>": world.apm_home,
            "<HOME>": world.home,
            "<TMP>": world.root.parent,
            "<RELEASE>": RELEASE_TAG,
        }

    def _normalize(self, text: str) -> str:
        return norm.normalize_text(text, self._paths())

    def _shell(self, line: str, world=None) -> CommandResult:
        world = world or self.world
        args = tuple(shlex.split(_sub(line)))
        done = subprocess.run(
            args,
            cwd=world.project,
            env=world.env,
            capture_output=True,
            text=True,
            timeout=driver.TIMEOUT_SECONDS,
            check=False,
        )
        return CommandResult(args, done.returncode, done.stdout, done.stderr, world.project)

    def _execute(self, line: str, world=None, scope: str = "project") -> _Command:
        world = world or self.world
        line = _sub(line)
        if line.split()[0] == "apm":
            result = driver.run(world, line, scope=scope)
        else:
            result = self._shell(line, world)
        return _Command(
            line, result, self._normalize(result.stdout), self._normalize(result.stderr)
        )

    def _text(self, n: int) -> str:
        return "\n".join(f"{c.stdout}\n{c.stderr}" for c in self.outputs.get(n, ()))

    # one step ---------------------------------------------------------------

    def run_step(self, step: dict, oracles: set[str]) -> list[tuple[str, str]]:
        """Run one step; return its (oracle, message) violations of the O-oracles."""
        n = step["n"]
        cells = self._cells(n)
        saved = dict(self.world.env)
        self.world.env.update({k: str(v) for k, v in (step.get("env") or {}).items()})
        try:
            self._prepare(step)
            # After a chdir the delta is the new project's: snapshot it (maybe absent) first.
            moved = (self.world.project / step["chdir"]).resolve() if step.get("chdir") else None
            start = self._roots(moved)
            commands: list[_Command] = []
            exits: list[tuple[_Command, int]] = []
            for line, code in zip(step["run"], _exits(step), strict=True):
                if line.split()[0] != "apm":
                    command = self._execute(line)
                    commands.append(command)
                    exits.append((command, code))
                    continue
                command = self._checked(step, line, cells)
                commands.append(command)
                exits.append((command, code))
                if "I3" in oracles and "audit --ci" in command.line:
                    # I3: install --frozen in the same state, paired with the audit's exit.
                    commands.append(self._checked(step, "apm install --frozen", cells, command))
            self.outputs[n] = commands
            self._finish(step)
            end = self._roots()
        finally:
            self.world.env.clear()
            self.world.env.update(saved)
        facts = _StepFacts(step, commands, exits, start, end)
        if self.record_dir is not None:
            # Every step's transcript, for journey authors (goldens are only the O1 .txt files).
            log = self.record_dir / self.journey["id"] / f"{n:02d}.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(
                "\n".join(
                    "\n".join(norm.render_command(c.line, c.result.returncode, c.stdout, c.stderr))
                    for c in commands
                )
                + "\n",
                encoding="utf-8",
            )
        violations = self._step_oracles(facts, oracles)
        self._pending[n] = self._credits(facts, oracles, violations)
        return violations

    def _roots(self, project: Path | None = None) -> ArtifactSnapshotSet:
        project = project or self.world.project
        return ArtifactSnapshotSet.capture({"project": project, "home": self.world.home})

    def _credits(self, facts: _StepFacts, oracles: set[str], violations) -> list[tuple]:
        """Index oracles of this step that passed with no violation (a known cell earns none)."""
        failed = {law for law, _ in violations}
        cell_laws = {
            c["law"] for c in self.journey.get("known_cells") or () if c["step"] == facts.step["n"]
        }
        credits = []
        for law in sorted(oracles):
            if law in failed or law in cell_laws or facts.last is None:
                continue  # a command-less (manual) step has no CLI result to credit
            if law in ORACLE_LAWS:
                # check_cross_cutting checked this invariant around every apm command.
                credits.append((law, Evidence(exit=facts.last.returncode), facts.last, facts))
            elif law in facts.evidence:
                evidence, result = facts.evidence[law]
                credits.append((law, evidence, result, facts))
        return credits

    def _checked(self, step, line: str, cells, paired: _Command | None = None) -> _Command:
        """One apm command with I1-I9 (check_cross_cutting) around it."""
        scope = step.get("scope", "project")
        args = driver.command_args(_sub(line))
        helps = "--help" in args or "--version" in args
        mutating = args[0] in MUTATING_COMMANDS and not helps and "--dry-run" not in args
        model = _JourneyModel(bool(step.get("converged")), cells, self.seen)
        before = self._observe(step)
        command = self._execute(line, scope=scope)
        after = self._observe(step)
        # The declared write set may depend on the outcome (a pin is recorded only on success).
        user_writes = self._user_writes(args, ok=command.result.returncode == 0)
        writes = bool(step.get("files")) or bool(step.get("chdir")) or bool(user_writes)
        op = operation_for(
            args,
            scope=scope,
            forbidden={"home": ("*",)} if scope == "project" else {},
            allowed={"home": (*_HOME_ALLOWED, *user_writes)},
            mutating=mutating,
            read_only=not mutating and not writes,
            paired={"audit": paired.result.returncode} if paired else {},
        )
        check_cross_cutting(before, after, command.result, model, op)
        return command

    def _user_writes(self, args: tuple[str, ...], *, ok: bool = True) -> tuple[str, ...]:
        """HOME-relative paths under APM_HOME a command declares it writes (I5)."""
        names = _APM_HOME_WRITES.get(args[0], ())
        if ok and "--dry-run" not in args:
            names = (*names, *_APM_HOME_WRITES_ON_SUCCESS.get(args[0], ()))
        if args[0] == "lifecycle" and args[1:2] not in (("trust",), ("untrust",)):
            names = ()
        if not names or "--help" in args:
            return ()
        try:
            base = self.world.apm_home.relative_to(self.world.home).as_posix()
        except ValueError:
            return ()  # APM_HOME outside HOME is not a snapshot root
        return tuple(f"{base}/{name}" for name in names)

    def _observe(self, step) -> Observation:
        project = self.world.project
        lock = None if step.get("drift") else LockFile.read(project / "apm.lock.yaml")
        managed: set[str] = set()
        if lock is not None:
            recorded = {
                path
                for dependency in lock.get_package_dependencies()
                for path in dependency.deployed_file_hashes
            }
            roots = {"/".join(path.split("/")[:2]) for path in recorded if "/" in path}
            for root in roots:
                managed.update(
                    p.relative_to(project).as_posix()
                    for p in (project / root).rglob("*")
                    if p.is_file()
                )
        cache = Path(self.world.env["APM_CACHE_DIR"])
        return Observation(
            state=LifecycleStateSnapshot.capture(
                project,
                surfaces=LifecycleSurfaceRoots(project, self.world.apm_home, cache, False),
            ),
            artifacts=ArtifactSnapshotSet.capture({"project": project, "home": self.world.home}),
            deployed=frozenset(),
            deploy_root=project,
            lock=lock,
            managed_paths=frozenset(managed),
        )

    def _cells(self, n: int) -> dict[str, tuple[ExpectedCell, ...]]:
        cells: dict[str, tuple[ExpectedCell, ...]] = {}
        for cell in self.journey.get("known_cells") or ():
            if cell["step"] == n and cell["law"] in ORACLE_LAWS:
                label = _label(cell)
                cells[cell["law"]] = (
                    *cells.get(cell["law"], ()),
                    ExpectedCell(label, cell["match"], per_step=False),
                )
        return cells

    # O-oracles --------------------------------------------------------------

    def _step_oracles(self, facts: _StepFacts, oracles: set[str]) -> list[tuple[str, str]]:
        """Evaluate each (journey, step, oracle) on its own; each returns its violations.

        ``_evaluate`` is the one place a per-oracle credit call
        (``oracles.check_obligation("J|<J>|<n>|<O>", ...)``) attaches.
        """
        step = facts.step
        checks: dict[str, object] = {
            "KEY": self._key,
            "FORBID": self._forbid,
            "O3": self._o3,
            "O4": self._o4,
            "O5": self._o5,
        }
        optional = {
            "O1": self._o1,
            "O2": self._o2,
            "O6": self._o6,
            "O7": self._o7,
            "O8a": self._o8a,
            "O8b": self._o8b,
            "O9": self._o9,
            "O10": self._o10,
            "O11": self._o11,
            "O12": self._o12,
            "O13": self._o13,
        }
        wanted = set(oracles)
        if (step.get("docs") or {}).get("output"):
            wanted.add("O2")
        if (step.get("docs") or {}).get("commands"):
            wanted.add("O13")
        if not any(c.result.returncode for c in facts.apm):
            wanted -= _ADVICE_LAWS  # O8a and O8b apply to red steps only
        checks.update({law: check for law, check in optional.items() if law in wanted})
        found: list[tuple[str, str]] = []
        for law, check in checks.items():
            found += self._evaluate(facts, law, check)
        return found

    def _evaluate(self, facts: _StepFacts, law: str, check) -> list[tuple[str, str]]:
        """One (journey, step, oracle): its violations; its evidence lands in ``facts``."""
        return [(law, message) for message in check(facts)]

    def _key(self, facts: _StepFacts) -> list[str]:
        keys = [_sub(k) for k in facts.step.get("key_lines") or ()]
        facts.record("KEY", Evidence(output_matches=[re.escape(k) for k in keys]))
        found = [f"key line not printed: {k!r}" for k in keys if k not in facts.text]
        for relative, wanted in (facts.step.get("file_lines") or {}).items():
            path = self.world.project / relative
            text = path.read_text(encoding="utf-8") if path.is_file() else ""
            found += [f"{relative} lacks {w!r}" for w in wanted if _sub(w) not in text]
        return found

    def _forbid(self, facts: _StepFacts) -> list[str]:
        found = []
        forbidden = [str(line) for line in facts.step.get("forbidden_lines") or ()]
        facts.record("FORBID", Evidence(output_forbids=[re.escape(line) for line in forbidden]))
        for line in forbidden:
            hits = [t.strip() for t in facts.text.splitlines() if line in t]
            if hits:
                found.append(f"forbidden {line!r} printed: {hits[0]!r}")
        return found

    def _o10(self, facts: _StepFacts) -> list[str]:
        warnings.warn(
            JourneyDeferredOracle(
                f"{self.journey['id']} step {facts.step['n']}: O10 twin fidelity needs the real "
                "upstream; it runs in the live node test_twin_fidelity_o10 (nightly, -m live)"
            ),
            stacklevel=2,
        )
        return []

    def _o11(self, facts: _StepFacts) -> list[str]:
        """Vendor files match the recorded vendor shape (``_VENDOR_SHAPES``).

        A step with commands checks the vendor files it wrote; a command-less
        (manual) step checks every vendor file the project holds.
        """
        project = self.world.project
        if facts.commands:
            candidates = sorted(set(_added(facts)) | _changed(facts))
        else:
            candidates = sorted(
                p.relative_to(project).as_posix() for p in project.rglob("*") if p.is_file()
            )
        checked: list[str] = []
        found: list[str] = []
        for relative in candidates:
            if relative.split("/")[0] in (*_UNTRACKED_ROOTS, "apm_modules"):
                continue
            for pattern, shape in _VENDOR_SHAPES:
                if fnmatch.fnmatchcase(relative, pattern):
                    checked.append(relative)
                    text = (project / relative).read_text(encoding="utf-8")
                    found += [f"{relative}: {problem}" for problem in shape(text)]
                    break
        if not checked:
            found.append("the step leaves no vendor file to validate")
        facts.record(
            "O11", Evidence(present={"project": checked}, expected=[], observed=list(found))
        )
        return found

    def _o1(self, facts: _StepFacts) -> list[str]:
        n = facts.step["n"]
        rendered = norm.render_golden(
            self.journey["id"],
            n,
            [
                norm.render_command(c.line, c.result.returncode, c.stdout, c.stderr)
                for c in facts.commands
            ],
        )
        rendered = self._normalize(rendered)
        if self.record_dir is not None:
            path = norm.golden_path(self.journey["id"], n, self.record_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered, encoding="utf-8")
            return []
        golden = norm.golden_path(self.journey["id"], n)
        if not golden.is_file():
            return [f"no golden {golden.relative_to(REPO).as_posix()}; run regen_goldens.py"]
        want = golden.read_text(encoding="ascii").splitlines()
        got = rendered.splitlines()
        facts.record("O1", Evidence(expected=norm.canonical(want), observed=norm.canonical(got)))
        if norm.canonical(want) == norm.canonical(got):
            return []
        diff = "\n".join(list(difflib.unified_diff(want, got, "golden", "run", lineterm=""))[:60])
        return [f"transcript differs from {golden.relative_to(REPO).as_posix()}:\n{diff}"]

    def _o2(self, facts: _StepFacts) -> list[str]:
        docs = facts.step.get("docs") or {}
        lines = [_o2_form(line) for line in docs.get("output") or () if line.strip() != "..."]
        if not lines:
            return []  # O2 applies only where the docs print an output block
        facts.record(
            "O2",
            Evidence(output_matches=[_o2_regex(line) for line in lines]),
            replace(
                facts.combined(normalized=True),
                stdout=_o2_text(facts, "stdout"),
                stderr=_o2_text(facts, "stderr"),
            ),
        )
        streams = [
            [_o2_form(t) for t in text.splitlines() if t.strip()]
            for c in facts.apm
            for text in (c.stdout, c.stderr)
        ]
        found = []
        positions = [0] * len(streams)
        for line in lines:
            hit = next((i for i, s in enumerate(streams) if line in s[positions[i] :]), None)
            if hit is None:
                found.append(f"docs line not printed in order: {line!r}")
                continue
            positions[hit] += streams[hit][positions[hit] :].index(line) + 1
        for stream in streams:
            for printed in stream:
                if printed.lstrip().startswith(("[!]", "[x]")) and printed not in lines:
                    found.append(f"extra diagnostic not in the docs block: {printed.strip()!r}")
        return found

    def _o3(self, facts: _StepFacts) -> list[str]:
        facts.record("O3", Evidence(ascii_output=True), facts.combined(normalized=False))
        return [
            f"{c.line!r} {name} is not ASCII"
            for c in facts.apm
            for name, text in (("stdout", c.result.stdout), ("stderr", c.result.stderr))
            if not text.isascii()
        ]

    def _o4(self, facts: _StepFacts) -> list[str]:
        allow = self.journey.get("allow_repeat") or ()
        found = []
        for command in facts.apm:
            # Help prose wraps freely; O7 owns help output, O4 owns diagnostics.
            helps = "--help" in driver.command_args(command.line)
            for name, text in (("stdout", command.stdout), ("stderr", command.stderr)):
                lines = [] if helps else text.splitlines()
                found += [
                    f"{command.line!r} {name} repeats {d!r}"
                    for d in norm.duplicate_lines(lines, allow)
                ]
                found += [
                    f"{command.line!r} {name} repeats a token: {t!r}"
                    for t in norm.adjacent_tokens(lines)
                ]
                for block in norm.package_blocks(lines):
                    pairs = [p for p in map(_kind_destination, block[1:]) if p]
                    repeated = sorted({p for p in pairs if pairs.count(p) > 1})
                    found += [f"{block[0].strip()!r} lists {p} twice" for p in repeated]
        facts.record("O4", Evidence(expected=[], observed=list(found)))
        return found

    def _o5(self, facts: _StepFacts) -> list[str]:
        found = [
            f"{c.line!r} exited {c.result.returncode}, step expects {code}"
            for c, code in facts.exits
            if c.result.returncode != code
        ]
        for command in facts.apm:
            code = command.result.returncode
            lines = f"{command.stdout}\n{command.stderr}".splitlines()
            errors = [line for line in lines if line.lstrip().startswith("[x]")]
            if errors and code == 0:
                found.append(f"{command.line!r} printed {errors[0].strip()!r} but exited 0")
            if code != 0 and not errors and not any("Error" in line for line in lines):
                found.append(f"{command.line!r} exited {code} without an [x] or error line")
            if "Usage:" in command.stderr and "Error:" in command.stderr and code != 2:
                found.append(f"{command.line!r} usage error exited {code}, not 2")
        last = facts.exits[-1] if facts.exits else None
        if last is not None:
            facts.record("O5", Evidence(exit=last[1]), last[0].result)
        return found

    def _o6(self, facts: _StepFacts) -> list[str]:
        step = facts.step
        if any(c.result.returncode for c in facts.apm):
            changed = [] if step.get("remediate_from") else sorted(facts.delta)
            facts.record("O6", Evidence(expected=[], observed=changed))
            return [f"failing step changed {changed}"] if changed else []
        found = []
        for path in sorted(facts.delta):
            if _declared(path, step):
                continue
            parts = path.split("/")
            names = [path] + ["/".join(parts[:k]) + "/" for k in range(2, len(parts))]
            if not any(name in facts.text for name in names):
                found.append(f"{path} changed but no output line announces it")
        facts.record(
            "O6", Evidence(present={"project": _added(facts)}, expected=[], observed=found)
        )
        return found

    def _o7(self, facts: _StepFacts) -> list[str]:
        step = facts.step
        experimental = self._experimental_commands()
        found = []
        for command in facts.apm:
            args = driver.command_args(command.line)
            if args == ("--help",):
                section = command.stdout.split("Common workflows:", 1)[-1]
                found += [
                    f"experimental 'apm {name}' in Common workflows"
                    for name in experimental
                    if re.search(rf"\bapm {name}\b", section)
                ]
            elif "--help" in args and step.get("help_docs"):
                shown, documented = help_flags(command.stdout), docs_flags(step["help_docs"])
                if shown != documented:
                    found.append(
                        f"apm {args[0]} --help vs {step['help_docs']}: only in help "
                        f"{sorted(shown - documented)}, only in docs {sorted(documented - shown)}"
                    )
            else:
                found += [
                    f"{command.line!r} suggests experimental 'apm {name}'"
                    for name in experimental
                    if re.search(rf"\bapm {name}\b", command.stdout)
                ]
        facts.record(
            "O7",
            Evidence(
                output_forbids=[
                    rf"\bapm {name}\b" for name in experimental if not _help_root(facts)
                ],
                expected=[],
                observed=list(found),
            ),
        )
        return found

    def _experimental_commands(self) -> tuple[str, ...]:
        if self._experimental is None:
            listing = self._execute("apm --help").stdout
            self._experimental = tuple(
                match.group(1)
                for match in re.finditer(r"^  (\S+)\s+.*\(experimental\)", listing, re.M)
            )
        return self._experimental

    def _o8a(self, facts: _StepFacts) -> list[str]:
        """Remediation: follow the printed advice in a copy; the failing command then passes."""
        step = facts.step
        if not step.get("remediate"):
            if not re.search(r"'apm [^']+'", facts.text):
                return ["the red step prints no 'apm ...' advice to follow"]
            return ["red step names O8a but has no remediate actions"]
        copy = self.world.root / f"o8-{self.journey['id']}-{step['n']}"
        shutil.copytree(self.world.project, copy, symlinks=True)
        probe = replace(self.world, project=copy)
        try:
            found = self._remediate(probe, step["remediate"], facts.text)
            rerun = step.get("rerun") or [c.line for c in facts.apm if c.result.returncode]
            for line in rerun:
                result = self._execute(line, probe).result
                if result.returncode != 0:
                    found.append(f"after the advice {line!r} still exits {result.returncode}")
                facts.record("O8a", Evidence(exit=0), result)
        finally:
            with contextlib.suppress(OSError):
                remove_tree(copy)
        return found

    def _o8b(self, facts: _StepFacts) -> list[str]:
        """Diagnostic: the printed advice reveals why the step failed; it does not fix it.

        In a clone of the world the advice runs, writes no durable state and exits 0
        or with the failing command's exit; it prints every promised line, none of
        which the failing step printed (strictly more information); and the failing
        command still exits as it did (the advice keeps the exit class).
        """
        spec = facts.step.get("diagnose")
        if not spec:
            return ["red step names O8b but has no diagnose advice"]
        failing = [c for c in facts.apm if c.result.returncode]
        code = failing[-1].result.returncode
        advice = _sub(spec["advice"])
        promises = [_sub(line) for line in spec.get("promises") or ()]
        found = [] if promises else ["diagnose names no promised information"]
        if f"'{advice}'" not in facts.text:
            found.append(f"advice '{advice}' is not printed")
        found += [f"the failing step already prints {p!r}" for p in promises if p in facts.text]
        # The same state as the failing step: a full clone of the world (project, HOME,
        # APM_HOME and caches) under the step's own environment.
        env = {**self.world.env, **{k: str(v) for k, v in (facts.step.get("env") or {}).items()}}
        destination = self.world.root.parent / f"o8b-{self.world.root.name}-{facts.step['n']}"
        probe = clone(replace(self.world, env=env), destination)
        try:
            before = self._durable(probe)
            ran = self._execute(advice, probe)
            after = self._durable(probe)
            if ran.result.returncode not in (0, code):
                found.append(f"advice {advice!r} exited {ran.result.returncode}, not 0 or {code}")
            text = f"{ran.stdout}\n{ran.stderr}"
            found += [f"advice {advice!r} does not print {p!r}" for p in promises if p not in text]
            found += [f"diagnostic advice changed {path}" for path in _raw_changes(before, after)]
            # The step's forbidden lines hold for the advice and the reruns too.
            forbidden = [_sub(str(line)) for line in facts.step.get("forbidden_lines") or ()]
            raw = f"{ran.result.stdout}\n{ran.result.stderr}"
            found += [f"advice {advice!r} prints forbidden {f!r}" for f in forbidden if f in raw]
            for command in failing:
                again = self._execute(command.line, probe).result
                if again.returncode != command.result.returncode:
                    found.append(
                        f"after the advice {command.line!r} exits {again.returncode}, "
                        f"not {command.result.returncode}"
                    )
                rerun = f"{again.stdout}\n{again.stderr}"
                found += [
                    f"rerun {command.line!r} prints forbidden {f!r}"
                    for f in forbidden
                    if f in rerun
                ]
            facts.record(
                "O8b",
                Evidence(
                    exit=ran.result.returncode,
                    output_matches=[re.escape(p) for p in promises],
                    output_forbids=[re.escape(f) for f in forbidden],
                ),
                ran.result,
            )
        finally:
            with contextlib.suppress(OSError):
                remove_tree(probe.root)
        return found

    @staticmethod
    def _durable(world) -> ArtifactSnapshotSet:
        """Every durable root a diagnostic command could write.

        The project, HOME, APM_HOME, the cache and every durable root the
        environment selects (tests/utils/isolated_apm_environment.py
        DURABLE_ENVIRONMENT_ROOTS: XDG, cache, GitHub and Azure config). A root
        inside another is covered by it.
        """
        candidates = {"project": world.project, "home": world.home, "apm_home": world.apm_home}
        if world.cache_root is not None:
            candidates["cache"] = world.cache_root
        for name in DURABLE_ENVIRONMENT_ROOTS:
            if world.env.get(name):
                candidates[name.lower()] = Path(world.env[name])
        resolved = {name: path.resolve() for name, path in candidates.items()}
        roots: dict[str, Path] = {}
        for name, path in sorted(resolved.items(), key=lambda item: len(item[1].parts)):
            if not any(path.is_relative_to(kept) for kept in roots.values()):
                roots[name] = path
        return ArtifactSnapshotSet.capture(roots)

    def _remediate(self, world, actions, text: str) -> list[str]:
        found = []
        for action in actions:
            if "delete" in action:
                paths = re.findall(action["delete"], text, re.M)
                if not paths:
                    found.append(f"the output lists no file for {action['delete']!r}")
                for path in paths:
                    (world.project / path).unlink()
            else:
                advice = _sub(action["advice"])
                if f"'{advice}'" not in text:
                    found.append(f"advice '{advice}' is not printed")
                result = self._execute(advice, world).result
                if result.returncode != 0:
                    found.append(f"advice {advice!r} exited {result.returncode}")
        return found

    def _o9(self, facts: _StepFacts) -> list[str]:
        patterns = [str(p) for p in facts.step.get("files") or ()]
        found = [
            f"unexpected change {path}"
            for path in sorted(facts.delta)
            if not any(_matches(path, p) for p in patterns)
        ]
        found += [
            f"expected change {pattern} did not happen"
            for pattern in patterns
            if not any(_matches(path, pattern) for path in facts.delta)
        ]
        status = ""
        if facts.step.get("git_clean"):
            status = self._shell("git status --porcelain").stdout.strip()
            if status:
                found.append(f"git status --porcelain is not empty: {status.splitlines()}")
        facts.record(
            "O9",
            Evidence(
                present={"project": _added(facts)},
                absent={"project": _removed(facts)},
                expected=sorted(patterns),
                observed=sorted(p for p in patterns if any(_matches(d, p) for d in facts.delta)),
            ),
        )
        return found

    def _o12(self, facts: _StepFacts) -> list[str]:
        log = self.world.services.egress_log()
        refused = sorted({entry["host"] for entry in log if entry["status"] == 502})
        # The fact: no request went to a host the world refuses (502).
        facts.record("O12", Evidence(egress=tuple(log), expected=[], observed=refused))
        return [f"contacted a host the world does not serve: {host}" for host in refused]

    def _o13(self, facts: _StepFacts) -> list[str]:
        docs = facts.step.get("docs")
        if not docs or not docs.get("commands"):
            return ["the step runs no command taken from a docs fence"]
        runs = [_sub(line) for line in facts.step["run"]]
        found = list(docs_problems(docs))
        for command in docs["commands"]:
            for part in _sub(command).split(" && "):
                ran = (
                    part[3:] == facts.step.get("chdir") if part.startswith("cd ") else part in runs
                )
                if not ran:
                    found.append(f"docs command {part!r} is not run literally")
        facts.record("O13", Evidence(expected=[], observed=list(found)))
        return found

    # settle -----------------------------------------------------------------

    def settle(self, n: int, violations: list[tuple[str, str]]) -> None:
        cells = [
            c
            for c in self.journey.get("known_cells") or ()
            if c["step"] == n and c["law"] in _O_LAWS
        ]
        unexpected = []
        for law, message in violations:
            owner = next(
                (c for c in cells if c["law"] == law and re.search(c["match"], message)), None
            )
            if owner is None:
                unexpected.append(f"{law}: {message}")
            else:
                self.seen.add(_label(owner))
        for cell in cells:
            if _label(cell) not in self.seen:
                unexpected.append(f"strict expected failure {_label(cell)} now passes; remove it")
        if unexpected:
            evidence = "\n".join(
                f"$ {c.line} [exit {c.result.returncode}]\n{c.stdout}\n{c.stderr}"
                for c in self.outputs.get(n, ())
            )
            pytest.fail(
                f"{self.journey['id']} step {n}:\n" + "\n".join(unexpected) + f"\n---\n{evidence}"
            )
        for law, evidence, result, facts in self._pending.pop(n, ()):
            check_obligation(
                f"J|{self.journey['id']}|{n}|{law}",
                strength="e2e",
                before=facts.before,
                after=facts.after,
                result=result,
                evidence=evidence,
            )

    def check_seen(self) -> None:
        strict = {_label(c) for c in self.journey.get("known_cells") or ()}
        missing = sorted(strict - self.seen)
        assert not missing, f"strict expected failures that no longer fail: {missing}"


# -- O11 recorded vendor shapes ------------------------------------------------
# Minimal shapes from each vendor's published format: required keys and, for
# Markdown primitives, the frontmatter keys the vendor reads.


def _json_object(text: str) -> tuple[dict, list[str]]:
    try:
        data = json.loads(text)
    except ValueError as exc:
        return {}, [f"not valid JSON: {exc}"]
    return (data, []) if isinstance(data, dict) else ({}, ["not a JSON object"])


def _sarif_shape(text: str) -> list[str]:
    """SARIF 2.1.0: version, runs[].tool.driver.name, results[].message.text."""
    data, problems = _json_object(text)
    if problems:
        return problems
    if data.get("version") != "2.1.0":
        problems.append(f"version {data.get('version')!r} is not 2.1.0")
    runs = data.get("runs")
    if not isinstance(runs, list) or not runs:
        return [*problems, "runs must be a non-empty list"]
    for run in runs:
        if not (run.get("tool") or {}).get("driver", {}).get("name"):
            problems.append("a run lacks tool.driver.name")
        for result in run.get("results") or ():
            if not (result.get("message") or {}).get("text"):
                problems.append("a result lacks message.text")
    return problems


def _claude_marketplace_shape(text: str) -> list[str]:
    """Claude Code marketplace.json: name, owner.name, plugins[] with name and source."""
    data, problems = _json_object(text)
    if problems:
        return problems
    if not isinstance(data.get("name"), str):
        problems.append("name must be a string")
    if not (data.get("owner") or {}).get("name"):
        problems.append("owner.name is required")
    plugins = data.get("plugins")
    if not isinstance(plugins, list):
        return [*problems, "plugins must be a list"]
    problems += [
        f"plugin {index} needs name and source"
        for index, plugin in enumerate(plugins)
        if not (isinstance(plugin, dict) and plugin.get("name") and plugin.get("source"))
    ]
    return problems


def _claude_plugin_shape(text: str) -> list[str]:
    """Claude Code plugin.json: a string name."""
    data, problems = _json_object(text)
    return problems or ([] if isinstance(data.get("name"), str) else ["name must be a string"])


def _mcp_json_shape(text: str) -> list[str]:
    """.mcp.json: mcpServers maps names to a command or a url."""
    data, problems = _json_object(text)
    servers = data.get("mcpServers")
    if problems or not isinstance(servers, dict):
        return problems or ["mcpServers must be an object"]
    return [
        f"server {name} needs command or url"
        for name, server in servers.items()
        if not (isinstance(server, dict) and (server.get("command") or server.get("url")))
    ]


def _frontmatter(text: str) -> dict | None:
    if not text.startswith("---\n"):
        return None
    try:
        data = yaml.safe_load(text.split("---\n", 2)[1])
    except (yaml.YAMLError, IndexError):
        return None
    return data if isinstance(data, dict) else None


def _markdown_shape(required: frozenset[str], allowed: frozenset[str] | None):
    def check(text: str) -> list[str]:
        front = _frontmatter(text)
        if front is None:
            return ["no YAML frontmatter"] if required else []
        problems = [f"frontmatter lacks {key}" for key in sorted(required - set(front))]
        if allowed is not None:
            problems += [
                f"frontmatter key {key!r} is not read" for key in sorted(set(front) - allowed)
            ]
        return problems

    return check


_VENDOR_SHAPES = (
    ("*.sarif", _sarif_shape),
    ("*.claude-plugin/marketplace.json", _claude_marketplace_shape),
    ("*.claude-plugin/plugin.json", _claude_plugin_shape),
    (".mcp.json", _mcp_json_shape),
    # Agent Skills: SKILL.md needs name and description.
    ("*skills/*/SKILL.md", _markdown_shape(frozenset({"name", "description"}), None)),
    (
        ".claude/commands/*.md",
        _markdown_shape(
            frozenset(),
            frozenset({"allowed-tools", "argument-hint", "description", "input", "model"}),
        ),
    ),
    (".claude/agents/*.md", _markdown_shape(frozenset({"name", "description"}), None)),
)


def _unreachable(services) -> set[str]:
    """Served ``owner/repo`` slugs that answer 503 (an outage), wrapping the handlers once."""
    slugs = getattr(services, "journey_unreachable", None)
    if slugs is not None:
        return slugs
    slugs = services.journey_unreachable = set()
    handlers = services.proxy._server.handlers
    for host in ("github.com", "api.github.com", "raw.githubusercontent.com"):
        original = handlers[host]

        def handler(request, original=original):
            parts = [p for p in request.path.split("/") if p]
            if parts[:1] == ["repos"]:
                parts = parts[1:]
            slug = "/".join(parts[:2]).removesuffix(".git")
            if slug in slugs:
                return ScriptedResponse(503, {"Content-Type": "text/plain"}, b"unavailable")
            return original(request)

        handlers[host] = handler
    return slugs


def _label(cell: Mapping) -> str:
    return f"{cell['id']} (tracking {cell['tracking']})"


def _sub(text: str) -> str:
    return str(text).replace("{release}", RELEASE_TAG)


def _exits(step: dict) -> list[int]:
    expected = step["exit"]
    return list(expected) if isinstance(expected, list) else [expected] * len(step["run"])


def _o2_form(line: str) -> str:
    return re.sub(r"\d+(?:\.\d+)?", "<N>", norm.normalize_text(line, {})).rstrip()


def _o2_regex(line: str) -> str:
    """A docs line (O2 form) as a regex over the O2-form output: <N> stands for a number."""
    return re.escape(line).replace(re.escape("<N>"), "<N>")


def _o2_text(facts: _StepFacts, stream: str) -> str:
    return "\n".join(_o2_form(line) for c in facts.apm for line in getattr(c, stream).splitlines())


def _help_root(facts: _StepFacts) -> bool:
    return any(driver.command_args(c.line) == ("--help",) for c in facts.apm)


def _added(facts: _StepFacts) -> list[str]:
    diff = facts.before.snapshot("project").diff(facts.after.snapshot("project"))
    return sorted(p for p in diff.added if p.split("/")[0] not in _UNTRACKED_ROOTS)


def _changed(facts: _StepFacts) -> set[str]:
    diff = facts.before.snapshot("project").diff(facts.after.snapshot("project"))
    return {p for p in diff.changed if p.split("/")[0] not in _UNTRACKED_ROOTS}


def _removed(facts: _StepFacts) -> list[str]:
    diff = facts.before.snapshot("project").diff(facts.after.snapshot("project"))
    return sorted(p for p in diff.removed if p.split("/")[0] not in _UNTRACKED_ROOTS)


def _kind_destination(child: str) -> tuple[str, str] | None:
    match = re.match(r"\s*\|--\s+(?:\d+\s+)?(.+?)\s+(?:integrated|adopted)\s+->\s+(.+)$", child)
    return (match.group(1), match.group(2)) if match else None


def _raw_changes(before: ArtifactSnapshotSet, after: ArtifactSnapshotSet) -> list[str]:
    """Every entry added, removed or changed in any root, unfiltered (no bookkeeping exempt)."""
    found = []
    for root_id, snapshot in before.snapshots:
        end = after.snapshot(root_id)
        if snapshot.root_existed != end.root_existed:
            # An empty root created or removed has no entries for diff() to report.
            found.append(f"{root_id}:<root {'created' if end.root_existed else 'removed'}>")
            continue
        diff = snapshot.diff(end)
        found += [f"{root_id}:{path}" for path in diff.added | diff.removed | diff.changed]
    return sorted(found)


def _delta(start: ArtifactSnapshot, end: ArtifactSnapshot) -> set[str]:
    """Changed project files; ``apm_modules`` collapses to one entry.

    ``.git`` and ``.apm-local`` (APM's self-ignored project bookkeeping) are not
    part of the delta; ``git_clean`` checks that they stay out of git status.
    """
    diff = start.diff(end)
    kinds = {e.relative_path: e.kind for e in (*start.entries, *end.entries)}
    paths = set()
    for path in diff.added | diff.removed | diff.changed:
        if path.split("/")[0] in _UNTRACKED_ROOTS or kinds.get(path) == "directory":
            continue
        paths.add("apm_modules/" if path.startswith("apm_modules/") else path)
    return paths


def _matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/"):
        return path.startswith(pattern)
    return fnmatch.fnmatchcase(path, pattern)


def _declared(path: str, step: dict) -> bool:
    """The command's own write set: manifest, lock, .gitignore, apm_modules, -o outputs."""
    outputs = {
        args[i + 1]
        for line in step["run"]
        for args in [shlex.split(_sub(line))]
        for i, arg in enumerate(args[:-1])
        if arg in ("-o", "--output")
    }
    return any(_matches(path, p) for p in _DECLARED_WRITES) or path in outputs


# -- tests -------------------------------------------------------------------


# Index oracles the runner never credits: O10 runs only in the live node.
_NEVER_CREDITED = frozenset({"O10"})
_ALWAYS_RECORDED = frozenset({"O1", "O3", "O4", "O5", "O6", "O7", "O9", "O11", "O12"})
_NODE = "tests/lifecycle/journeys/test_journeys.py::test_journey[{}]"


def _credited(step: dict, law: str) -> bool:
    """Whether a green run credits *law* on *step* (mirrors ``_step_oracles`` and ``_credits``)."""
    if law in _NEVER_CREDITED or not step.get("run"):
        return False
    docs = step.get("docs") or {}
    if law in ORACLE_LAWS or law in _ALWAYS_RECORDED:
        return True
    if law == "O2":
        return any(str(line).strip() != "..." for line in docs.get("output") or ())
    if law in _ADVICE_LAWS:
        return any(
            line.split()[0] == "apm" and code != 0
            for line, code in zip(step["run"], _exits(step), strict=True)
        )
    if law == "O13":
        return bool(docs.get("commands"))
    return False


def journey_witnesses() -> tuple[dict[str, str], dict[str, str]]:
    """``(required, known_failing)`` journey obligation ids, decided statically.

    required: ``J|<J>|<n>|<O>`` ids a green run of ``test_journey[<J>]`` credits,
    mapped to that node. known_failing: ids owned by a strict known cell of the
    same law with a public issue (``#<n>``, n > 0), mapped to the issue. Cells
    tracked as ``#0`` and oracles the runner cannot credit stay in the baseline.
    """
    index = {j["id"]: j for j in load_index()["journeys"]}
    required: dict[str, str] = {}
    known: dict[str, str] = {}
    for path in journey_files():
        journey = load_journey(path)
        jid = journey["id"]
        cells = journey.get("known_cells") or ()
        for step, spec in zip(journey["steps"], index[jid]["steps"], strict=True):
            n = step["n"]
            cell_laws = {c["law"] for c in cells if c["step"] == n}
            for law in spec["oracles"]:
                key = f"J|{jid}|{n}|{law}"
                owners = [c for c in cells if c["step"] == n and c["law"] == law]
                public = [
                    c["tracking"] for c in owners if re.fullmatch(r"#[1-9]\d*", c["tracking"])
                ]
                if public:
                    known[key] = public[0]
                elif law not in cell_laws and _credited(step, law):
                    required[key] = _NODE.format(jid)
    return dict(sorted(required.items())), dict(sorted(known.items()))


# Strict Windows-only expected failures of whole journeys, each tracked by an issue.
_WINDOWS_KNOWN: dict[str, KnownFailure] = {}


def _params() -> list:
    golden = {j["id"] for j in load_index()["journeys"] if j.get("golden")}
    params = []
    for path in journey_files():
        jid = load_journey(path)["id"]
        marks = [pytest.mark.lifecycle_smoke] if jid in golden else []
        if jid in _WINDOWS_KNOWN and os.name == "nt":
            marks.append(_WINDOWS_KNOWN[jid].mark())
        params.append(pytest.param(path, id=jid, marks=marks))
    return params


@pytest.fixture(scope="session")
def journey_twins(lifecycle_template, tmp_path_factory) -> Mapping[str, Path]:
    """Content twins of the upstream repositories, served by the world (once per session).

    Every ``twins_<group>.py`` beside this module plugs in by exporting
    ``publish_<group>_twins(services, root, env) -> dict[str, Path]``.
    """
    root = tmp_path_factory.mktemp("journey-twins")
    services, env = lifecycle_template.services, lifecycle_template.env
    published = dict(publish_twins(services, root / "core", env))
    for path in sorted(HERE.glob("twins_*.py")):
        group = path.stem.removeprefix("twins_")
        module = importlib.import_module(f"tests.lifecycle.journeys.{path.stem}")
        publish = getattr(module, f"publish_{group}_twins")
        added = publish(services, root / path.stem, env)
        clash = set(added) & set(published)
        assert not clash, f"{path.name} republishes {sorted(clash)}"
        published.update(added)
    return published


def test_journey_files_agree_with_the_index() -> None:
    index = load_index()
    problems = [p for path in journey_files() for p in check_journey_file(path, index)]
    for path in journey_files():
        for step in load_journey(path)["steps"]:
            if step.get("docs"):
                problems += [
                    f"{path.stem} step {step['n']}: {p}" for p in docs_problems(step["docs"])
                ]
    assert problems == []


@pytest.mark.parametrize("variable", ["APM_CACHE_DIR", "XDG_CONFIG_HOME", "GH_CONFIG_DIR"])
def test_o8b_sees_a_write_under_an_environment_root_outside_home(
    lifecycle_world, variable: str
) -> None:
    """O8b's no-write check covers every durable root the environment selects."""
    world = lifecycle_world
    selected = Path(world.env.get(variable) or world.root / variable.lower())
    selected.mkdir(parents=True, exist_ok=True)
    probe = replace(world, env={**world.env, variable: str(selected)})
    assert not selected.resolve().is_relative_to(world.home.resolve()), "a root outside HOME"
    written = selected / "written-by-advice.txt"
    assert not written.exists(), "the root starts without the file"
    before = JourneyRun._durable(probe)
    written.write_text("x\n", encoding="ascii")
    after = JourneyRun._durable(probe)

    changes = _raw_changes(before, after)
    assert any(path.endswith("written-by-advice.txt") for path in changes), changes


@pytest.mark.lifecycle_smoke
def test_o8b_no_write_check_sees_an_empty_durable_root_created(lifecycle_world) -> None:
    """Creating an empty durable root is a write, though it adds no entry."""
    world = lifecycle_world
    selected = world.root / "gh-config-created-by-advice"
    probe = replace(world, env={**world.env, "GH_CONFIG_DIR": str(selected)})
    assert not selected.exists(), "the root starts absent"
    before = JourneyRun._durable(probe)
    selected.mkdir()
    after = JourneyRun._durable(probe)

    changes = _raw_changes(before, after)
    assert any(path.endswith(":<root created>") for path in changes), changes


@pytest.mark.parametrize("path", _params())
def test_journey(path: Path, lifecycle_world, journey_twins) -> None:
    del journey_twins
    index = load_index()
    assert check_journey_file(path, index) == []
    journey = load_journey(path)
    entry = next(j for j in index["journeys"] if j["id"] == journey["id"])
    record = os.environ.get(RECORD_ENV)
    run = JourneyRun(lifecycle_world, journey, entry, Path(record) if record else None)
    try:
        run.start()
        for step, spec in zip(journey["steps"], entry["steps"], strict=True):
            oracles = set(spec["oracles"])
            run.settle(step["n"], run.run_step(step, oracles))
        run.check_seen()
    finally:
        run.close()


@pytest.mark.live
@pytest.mark.parametrize("slug", sorted(TWINS))
def test_twin_fidelity_o10(slug: str, tmp_path: Path) -> None:
    """O10 (nightly, network): a twin has the upstream's primitive layout at the documented ref."""
    twin = TWINS[slug]
    upstream = tmp_path / "upstream"
    clone = subprocess.run(
        (
            "git",
            "clone",
            "-q",
            "--depth",
            "1",
            "--branch",
            twin.ref,
            f"https://github.com/{slug}",
            str(upstream),
        ),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert clone.returncode == 0, clone.stderr
    if twin.copy_from:
        want = {
            p.relative_to(REPO / twin.copy_from).as_posix()
            for p in (REPO / twin.copy_from).rglob("*")
            if p.is_file()
        }
        got = {
            p.relative_to(upstream / twin.copy_to).as_posix()
            for p in (upstream / twin.copy_to).rglob("*")
            if p.is_file()
        }
        assert got == want
        return
    primitives = {p for p in twin.files if p.startswith((".apm/", "skills/")) or p == "apm.yml"}
    for relative in primitives:
        assert (upstream / relative).is_file(), f"{slug}@{twin.ref} has no {relative}"
        assert _front_keys((upstream / relative).read_text(encoding="utf-8")) == _front_keys(
            twin.files[relative]
        )
    if "apm.yml" in primitives:
        real = yaml.safe_load((upstream / "apm.yml").read_text(encoding="utf-8"))
        assert real.get("dependencies") == yaml.safe_load(twin.files["apm.yml"]).get("dependencies")


def _front_keys(text: str) -> set[str]:
    if not text.startswith("---\n"):
        return set()
    front = text.split("---\n", 2)[1]
    return {line.split(":", 1)[0] for line in front.splitlines() if re.match(r"^[a-zA-Z]", line)}
