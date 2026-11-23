"""Install engine parity: the fast engine and the full pipeline leave identical state.

PROMISE: P-INV-I2 -- repeating a converged install changes no bytes, whichever
engine answers (source: tests/lifecycle/oracles.py).
PROMISE: P-INV-I1 -- the lockfile matches the deployed files after every step.
PROMISE: P-INV-I4 -- a stricter policy, an edited or deleted deployed file, a
changed manifest, a lockfile from another apm version, a store miss or a
non-default flag is honored by the next install: the fast engine declines and
the full pipeline decides ("any doubt = full pipeline", Lane C plan).

LANDING GATE for lane C S0 (no-op engine) and S2 (diff-apply engine).

DIFFERENTIAL ORACLE. Two clones of one world run the same steps: the fast
world with ``APM_INSTALL_FAST_PATH=trace`` (one decision line on stderr per
install), the full world with ``APM_INSTALL_FAST_PATH=0`` (every fast engine
off). After every step the exit code, the output (paths, timings and the
decision line normalized away) and ``_deploy_diff.comparable`` (project and
HOME bytes, manifest, lock; identity aside) must match. The change table and
the state oracle are shared with W4's incremental-vs-fresh suite.

DECISIONS (engine contract agreed with the no-op and diff-apply owners):
``no-op (...)``, ``applied a verified diff (<n> files)`` or ``full run
(<reason>)``. A cell names the decision it requires, so it is red while the
engine owning that decision is absent: a ``KnownFailure`` (tracking #4770,
signature: the missing decision) until the engine module exists, then required. Only
the missing decision is the expected failure: a parity break or a failed
command fails the cell even at main.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

from apm_cli.deps.lockfile import LockFile
from tests.lifecycle import driver
from tests.lifecycle.known import KnownFailure
from tests.lifecycle.scenarios._deploy_diff import (
    CHANGES,
    comparable,
    converge,
    diff,
    digest,
    revoke_user_approval,
)
from tests.lifecycle.world import World
from tests.lifecycle.world.clone import clone
from tests.lifecycle.world.scopes import use_policy, use_registry
from tests.lifecycle.world.services import (
    ORG,
    POLICY_FILE,
    POLICY_REPO,
    policy_documents,
    policy_org,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise("P-INV-I1", "P-INV-I2", "P-INV-I4"),
    pytest.mark.skipif(sys.platform == "win32", reason="fast engines are POSIX-only"),
]

FAST_ENV = "APM_INSTALL_FAST_PATH"
NOOP = "no-op"
DIFF = "diff"
FULL = "full"
_DECISION = re.compile(r"^\[i\] install fast path: (.*)$", re.MULTILINE)
_ELAPSED = re.compile(r"\b\d+(?:\.\d+)?m?s\b")
_PARALLEL = re.compile(r"^\s*\[>\] Resolving ")
# Policy freshness depends on when each world last fetched it (as #4775 normalized).
_POLICY_AGE = re.compile(r" \(cached, fetched \d+s ago\)")
BOTH = ("p-skill:git", "p-exec:git")
# The deploy store (#4846): <APM_CACHE_DIR>/deploy_v1/<key[:2]>/<key>.
DEPLOY_BUCKET = "deploy_v1"
POLICY_CACHE = "policy_v1"  # policy/discovery.py POLICY_CACHE_DIR


def _present(*modules: str) -> bool:
    return any(importlib.util.find_spec(name) is not None for name in modules)


NOOP_ENGINE = _present("apm_cli.install.noop_diff", "apm_cli.install.desired_state")
DIFF_ENGINE = _present("apm_cli.install.diff_apply")
# S4 (a fresh, lockfile-less install served from a warm cache) has its own seam.
FRESH_ENGINE = _present("apm_cli.install.fresh_apply")
ANY_ENGINE = NOOP_ENGINE or DIFF_ENGINE


class MissingDecision(AssertionError):
    """The fast world did not take the decision the cell requires."""


# Lane C engines (S0 no-op, S2 diff-apply) are tracked by one open issue.
_ENGINE_ISSUE = "#4770"
_MISSING = r"expected a [a-z/-]+ decision, got"


@dataclass(frozen=True)
class Needs:
    """Red-first marks of a cell owned by an engine: known-failing while it is absent."""

    marks: tuple[pytest.MarkDecorator, ...]

    def __call__(self, test: Callable) -> Callable:
        for mark in self.marks:
            test = mark(test)
        return test


def _needs(present: bool, engine: str) -> Needs:
    if present:
        return Needs(())
    known = KnownFailure(
        _ENGINE_ISSUE,
        f"the {engine} install engine is not on this branch yet",
        _MISSING,
        kind="deferred-design",
        raises=MissingDecision,
    )
    return Needs((known.mark(),))


NEEDS_NOOP = _needs(NOOP_ENGINE, "no-op")
NEEDS_DIFF = _needs(DIFF_ENGINE, "diff-apply")
NEEDS_FRESH = _needs(FRESH_ENGINE, "fresh-apply")
NEEDS_ANY = _needs(ANY_ENGINE, "fast")


def _kind(decision: str) -> str:
    if decision.startswith("no-op"):
        return NOOP
    if decision.startswith("applied a verified diff"):
        return DIFF
    if decision.startswith("full run"):
        return FULL
    return f"none ({decision!r})"


def _assert_decision(decision: str, *kinds: str) -> None:
    if _kind(decision) not in kinds:
        raise MissingDecision(f"expected a {'/'.join(kinds)} decision, got {decision!r}")


def _normalized(world: World, text: str) -> list[str]:
    """Output without the decision line, the world root and timings.

    Dependencies resolve in parallel, so each run of consecutive progress lines
    ("[>] Resolving ...") is compared as a sorted block; all other order counts.
    """
    text = _POLICY_AGE.sub("", _DECISION.sub("", text)).replace(str(world.root), "<ROOT>")
    text = _ELAPSED.sub("<T>", text)
    lines: list[str] = []
    block: list[str] = []
    for line in [line for line in text.splitlines() if line.strip()] + [""]:
        if _PARALLEL.match(line):
            block.append(line)
            continue
        lines += sorted(block) + ([line] if line else [])
        block = []
    return lines


# -- the twin -------------------------------------------------------------------


@dataclass
class Twin:
    fast: World
    full: World
    codes: list[int] = field(default_factory=list)
    evidence: str = ""

    @property
    def worlds(self) -> tuple[World, World]:
        return (self.fast, self.full)

    def each(self, change: Callable[[World], object]) -> None:
        for world in self.worlds:
            change(world)

    def invoke(self, world: World, argv: tuple[str, ...]):
        """Run *argv* in one world with that world's engine mode."""
        world.env[FAST_ENV] = "trace" if world is self.fast else "0"
        try:
            return driver.run(world, argv)
        finally:
            world.env.pop(FAST_ENV, None)

    def run(self, *argv: str, exempt: tuple[str, ...] = ()) -> str:
        """Run *argv* in both worlds, assert parity, return the fast world's decision."""
        fast, full = (self.invoke(world, argv) for world in self.worlds)
        decisions = _DECISION.findall(fast.stderr)
        assert len(decisions) <= 1, f"one decision per command, got {decisions}"
        label = " ".join(argv)
        evidence = f"\n--- fast\n{fast.stdout}{fast.stderr}\n--- full\n{full.stdout}{full.stderr}"
        assert fast.returncode == full.returncode, f"{label}: exit codes differ{evidence}"
        for stream in ("stdout", "stderr"):
            assert _normalized(self.fast, getattr(fast, stream)) == _normalized(
                self.full, getattr(full, stream)
            ), f"{label}: {stream} differs from the full pipeline{evidence}"
        problems = diff(
            comparable(self.fast, exempt),
            comparable(self.full, exempt),
            self.worlds,
            ("fast", "full"),
        )
        assert not problems, f"{label}: state differs from the full pipeline\n" + "\n".join(
            problems
        )
        self.codes.append(fast.returncode)
        self.evidence = evidence
        return decisions[0] if decisions else ""

    def ok(self, *argv: str) -> str:
        decision = self.run(*argv)
        assert self.codes[-1] == 0, f"{' '.join(argv)} failed in both worlds{self.evidence}"
        return decision


@pytest.fixture
def twin(lifecycle_template: World, tmp_path: Path) -> Twin:
    pair = Twin(
        clone(lifecycle_template, tmp_path / "f"), clone(lifecycle_template, tmp_path / "r")
    )
    pair.fast.services.reset()  # one service set per worker: both worlds share it
    pair.each(lambda w: use_policy(w, "allow"))
    return pair


def _declare(world: World, *keys: str) -> None:
    manifest = world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    data.setdefault("dependencies", {})["apm"] = [world.packages[k].spec for k in keys]
    manifest.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _converge(twin: Twin, *keys: str) -> None:
    twin.each(lambda w: _declare(w, *keys))
    twin.ok("install")
    if "p-exec:git" in keys:
        twin.ok("approve", f"{ORG}/p-exec")
        twin.ok("install")


def _deployed(world: World, package: str = "") -> list[Path]:
    lock = LockFile.read(world.project / "apm.lock.yaml")
    assert lock is not None, "no lockfile"
    paths = [
        world.project / rel
        for key, dep in lock.dependencies.items()
        if package in key
        for rel in sorted(dep.deployed_file_hashes)
        if (world.project / rel).is_file()
    ]
    assert paths, f"the lock records no hashed deployed file of {package or 'any package'}"
    return paths


def _publish_skill(twin: Twin, body: str, tag: str) -> None:
    source = Path(twin.fast.packages["p-skill"].spec)
    skill = next(source.rglob("SKILL.md")).relative_to(source).as_posix()
    text = f"---\nname: p-skill\ndescription: lifecycle skill.\n---\n\n{body}\n"
    twin.fast.services.publish_revision(f"{ORG}/p-skill", {skill: text}, tag=tag)


def _remove_deployed(world: World) -> None:
    for path in _deployed(world):
        path.unlink()
    shutil.rmtree(world.project / "apm_modules", ignore_errors=True)


def _checkout(world: World, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        (world.project / rel).write_bytes(data)


def _snapshot(world: World) -> dict[str, bytes]:
    return {rel: (world.project / rel).read_bytes() for rel in ("apm.yml", "apm.lock.yaml")}


# -- journeys the engines claim -------------------------------------------------


@NEEDS_NOOP
def test_reinstall_of_a_converged_project_is_a_noop(twin: Twin) -> None:
    _converge(twin, *BOTH)
    _assert_decision(twin.ok("install"), NOOP)
    _assert_decision(twin.ok("install"), NOOP)


@NEEDS_NOOP
def test_frozen_reinstall_of_a_converged_project_is_a_noop(twin: Twin) -> None:
    _converge(twin, *BOTH)
    locks = {w.root: (w.project / "apm.lock.yaml").read_bytes() for w in twin.worlds}
    decision = twin.ok("install", "--frozen")
    for world in twin.worlds:
        assert (world.project / "apm.lock.yaml").read_bytes() == locks[world.root], "frozen rewrote"
    _assert_decision(decision, NOOP)


@NEEDS_ANY
def test_install_after_uninstall_matches_the_full_pipeline(twin: Twin) -> None:
    _converge(twin, *BOTH)
    twin.ok("uninstall", f"{ORG}/p-skill")
    _assert_decision(twin.ok("install"), NOOP, FULL)


@NEEDS_DIFF
def test_warm_locked_install_applies_the_diff(twin: Twin) -> None:
    """Lockfile and store present, deployed tree gone: a fresh checkout on a warm machine."""
    _converge(twin, *BOTH)
    twin.each(_remove_deployed)
    _assert_decision(twin.ok("install"), DIFF)


@NEEDS_DIFF
def test_frozen_ci_on_a_warm_store_applies_the_diff(twin: Twin) -> None:
    _converge(twin, *BOTH)
    twin.each(_remove_deployed)
    _assert_decision(twin.ok("install", "--frozen"), DIFF)


@NEEDS_ANY
def test_branch_switch_lockfile_swap_matches_the_full_pipeline(twin: Twin) -> None:
    """Install v1, move to v2, then check out v1's manifest and lockfile again.

    The full pipeline keeps v2's bytes as a local edit of the swapped-in lock
    ("1 local edit(s) to managed files"), so a diff decision is valid only if
    it lands the same bytes; declining to FULL is the parity-safe answer.
    """
    _converge(twin, "p-skill:git")
    v1 = {w.root: _snapshot(w) for w in twin.worlds}
    _publish_skill(twin, "Version two.", tag="v2.0.0")

    def to_v2(world: World) -> None:
        manifest = world.project / "apm.yml"
        text = manifest.read_text(encoding="utf-8")
        manifest.write_text(text.replace("p-skill#v1.0.0", "p-skill#v2.0.0"), encoding="utf-8")

    twin.each(to_v2)
    twin.ok("install")
    twin.each(lambda w: _checkout(w, v1[w.root]))
    _assert_decision(twin.ok("install"), DIFF, FULL)


@NEEDS_ANY
def test_lockfile_that_drops_a_deployed_package_declines_to_the_full_pipeline(
    twin: Twin,
) -> None:
    """Branch switch to a lockfile without p-exec, whose files are still deployed (orphans).

    The fast world must end byte-identical to the full pipeline (the same files
    deleted or kept), with a user file outside the ledger untouched.
    """
    _converge(twin, "p-skill:git")
    without_exec = {w.root: _snapshot(w) for w in twin.worlds}
    _converge(twin, *BOTH)

    def switch(world: World) -> None:
        (world.project / "notes.md").write_bytes(b"the user's own file\n")
        _checkout(world, without_exec[world.root])

    twin.each(switch)
    decision = twin.ok("install")
    for world in twin.worlds:
        assert (world.project / "notes.md").read_bytes() == b"the user's own file\n"
    _assert_decision(decision, FULL)


def test_update_matches_the_full_pipeline(twin: Twin) -> None:
    """No engine claims ``update``: parity guard, green at main, must stay green."""
    _converge(twin, "p-skill:git")
    _publish_skill(twin, "Moved tag.", tag="v1.0.0")
    _assert_decision(twin.ok("update", "--yes") or "full run (not install)", FULL)


# -- cleanup on a hit and on a doubt (the engine is pre-pipeline) -------------------


def _user_and_orphan_files(world: World) -> dict[str, bytes]:
    """A user file next to a claimed file, plus an unclaimed file in a managed root."""
    claimed = _deployed(world, "p-skill")[0]
    extra = {
        claimed.parent.relative_to(world.project).as_posix() + "/user-notes.md": b"mine\n",
        ".agents/skills/p-gone/SKILL.md": b"---\nname: p-gone\ndescription: stale.\n---\n",
    }
    for rel, data in extra.items():
        path = world.project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return extra


@NEEDS_NOOP
def test_noop_hit_changes_no_project_file(twin: Twin) -> None:
    """A hit skips lockfile, cleanup and finalize: not one project byte moves."""
    _converge(twin, *BOTH)
    twin.each(_user_and_orphan_files)
    before = digest(twin.fast.project, (".git",), twin.fast)
    decision = twin.ok("install")
    _assert_decision(decision, NOOP)
    assert digest(twin.fast.project, (".git",), twin.fast) == before, "a no-op hit wrote"


@NEEDS_ANY
def test_cleanup_on_a_doubt_prunes_like_the_full_pipeline(twin: Twin) -> None:
    """Dropping p-exec from apm.yml is a doubt: FULL prunes it; user and unclaimed files stay."""
    _converge(twin, *BOTH)
    extra = {w.root: _user_and_orphan_files(w) for w in twin.worlds}
    twin.each(lambda w: _declare(w, "p-skill:git"))
    decision = twin.ok("install")
    for world in twin.worlds:
        for rel, data in extra[world.root].items():
            assert (world.project / rel).read_bytes() == data, f"cleanup touched {rel}"
        assert _deployed(world, "p-skill"), "a still-claimed file was pruned"
    _assert_decision(decision, FULL)


# -- W4's change table: change -> install, fast == full -----------------------------


@pytest.mark.parametrize(
    "change", [pytest.param(c, marks=NEEDS_ANY.marks) for c in sorted(CHANGES)]
)
def test_install_after_a_change_matches_the_full_pipeline(twin: Twin, change: str) -> None:
    """Same history in both worlds, so nothing is exempt: an edited or symlinked path is compared."""
    spec = CHANGES[change]
    twin.each(lambda w: converge(w, spec.start, run=twin.invoke))
    twin.each(spec.apply)
    outside = {w.root: digest(w.root / "outside-skill", (), w) for w in twin.worlds}
    decision = twin.run("install")
    for world in twin.worlds:
        after = digest(world.root / "outside-skill", (), world)
        assert after == outside[world.root], "install wrote through a symlinked destination"
    _assert_decision(decision, DIFF, FULL)


# -- doubt triggers: the fast engine must decline ------------------------------------


def _edit_deployed(world: World) -> None:
    path = _deployed(world)[0]
    path.write_bytes(path.read_bytes() + b"\nuser edit\n")


def _drop_exec_from_manifest(world: World) -> None:
    _declare(world, "p-skill:git")


def _stricter_policy(world: World) -> None:
    use_policy(world, "deny-pkg")  # denies p-exec


def _lock_from_another_apm(world: World) -> None:
    lock = world.project / "apm.lock.yaml"
    data = yaml.safe_load(lock.read_text(encoding="utf-8"))
    data["apm_version"] = "0.0.1"
    lock.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _cache_roots(world: World) -> list[Path]:
    roots = {world.cache_root, world.env.get("APM_CACHE_DIR")}
    return [Path(root) for root in roots if root]


def _empty_store(world: World) -> None:
    _remove_deployed(world)
    for root in _cache_roots(world):
        shutil.rmtree(root, ignore_errors=True)


def _tamper_store(world: World) -> None:
    """Corrupt every deploy-store entry, then remove the deployed tree."""
    _remove_deployed(world)
    entries = [
        entry
        for root in _cache_roots(world)
        for entry in sorted((root / DEPLOY_BUCKET).glob("*/*"))
        if entry.is_file()
    ]
    assert entries or not DIFF_ENGINE, "the deploy store holds no entry to tamper"
    for entry in entries:
        entry.write_bytes(entry.read_bytes() + b"\ntampered\n")


def _nothing(world: World) -> None:
    del world


DOUBTS: dict[str, tuple[Callable[[World], None], tuple[str, ...], int]] = {
    # trigger: (change after converging, install argv, exit code of the full pipeline)
    "apm-version-changed": (_lock_from_another_apm, ("install",), 0),
    "apm-yml-changed": (_drop_exec_from_manifest, ("install",), 0),
    "deployed-file-edited": (_edit_deployed, ("install",), 0),
    "non-default-flag": (_nothing, ("install", "--force"), 0),
    "policy-changed": (_stricter_policy, ("install",), 1),
    "store-miss": (_empty_store, ("install",), 0),
    "store-entry-tampered": (_tamper_store, ("install",), 0),
}


@pytest.mark.parametrize(
    "trigger", [pytest.param(t, marks=NEEDS_ANY.marks) for t in sorted(DOUBTS)]
)
def test_doubt_trigger_declines_to_the_full_pipeline(twin: Twin, trigger: str) -> None:
    change, argv, code = DOUBTS[trigger]
    _converge(twin, *BOTH)
    twin.each(change)
    decision = twin.run(*argv)
    assert twin.codes[-1] == code, (
        f"{trigger}: exit {twin.codes[-1]}, expected {code}{twin.evidence}"
    )
    _assert_decision(decision, FULL)


@NEEDS_ANY
def test_external_scanners_flag_declines_to_the_full_pipeline(twin: Twin) -> None:
    _converge(twin, *BOTH)
    twin.ok("experimental", "enable", "external_scanners")
    _assert_decision(twin.ok("install"), FULL)


@pytest.mark.parametrize(
    "char",
    [
        pytest.param("\u200b", id="zero-width-space", marks=NEEDS_ANY.marks),
        pytest.param("\ufeff", id="mid-file-bom", marks=NEEDS_ANY.marks),
        pytest.param("\u202e", id="bidi-override", marks=NEEDS_ANY.marks),
    ],
)
def test_hidden_unicode_in_a_deployed_file_takes_the_full_scan(twin: Twin, char: str) -> None:
    """The ASCII guard: a non-ASCII hidden character means a full scan, same findings."""
    _publish_skill(twin, f"Use p{char}skill. Hidden character mid-file.", tag="v1.0.0")
    twin.each(lambda w: _declare(w, "p-skill:git"))
    twin.run("install")  # a critical finding may block: parity holds either way
    _assert_decision(twin.run("install"), FULL)


def _local_path_source(twin: Twin) -> None:
    _converge(twin, "p-skill")


def _marketplace_source(twin: Twin) -> None:
    """Marketplace specs go through ``apm install <spec>``, which records the resolved form."""
    twin.ok("marketplace", "add", twin.fast.services.marketplace_url)
    twin.ok("install", twin.fast.packages["p-skill:marketplace"].spec)


def _registry_source(twin: Twin) -> None:
    twin.each(use_registry)
    _converge(twin, "p-skill:registry")


SOURCES: dict[str, Callable[[Twin], None]] = {
    "local-path": _local_path_source,
    "marketplace": _marketplace_source,
    "registry": _registry_source,
}


@pytest.mark.parametrize(
    "source", [pytest.param(s, marks=NEEDS_ANY.marks) for s in sorted(SOURCES)]
)
def test_source_without_an_immutable_pin_declines_to_the_full_pipeline(
    twin: Twin, source: str
) -> None:
    SOURCES[source](twin)
    _assert_decision(twin.ok("install"), FULL)


# -- Git-only security baselines: prove a hit, then the trust change must decline ----


def _clear_policy_cache(world: World) -> None:
    for root in _cache_roots(world):
        shutil.rmtree(root / POLICY_CACHE, ignore_errors=True)


def _deny_executables_at_the_same_origin(twin: Twin) -> None:
    """Serve exec-deny-all from the project's existing policy origin (no remote change)."""
    document = yaml.safe_dump(policy_documents()["exec-deny-all"] | {"name": "allow"})
    twin.fast.services.publish_revision(
        f"{policy_org('allow')}/{POLICY_REPO}", {POLICY_FILE: document}
    )
    twin.each(_clear_policy_cache)  # the next install rediscovers the policy live


def _converge_exec_user_approved(twin: Twin) -> None:
    twin.each(lambda w: _declare(w, "p-exec:git"))
    twin.ok("install")
    twin.ok("approve", "--user", "p-exec")
    twin.ok("install")


TRUST_CHANGES: dict[str, Callable[[Twin], None]] = {
    "approval-revoked": lambda twin: twin.each(revoke_user_approval),
    "executables-denied-at-same-origin": _deny_executables_at_the_same_origin,
}


@pytest.mark.parametrize(
    "trust", [pytest.param(t, marks=NEEDS_NOOP.marks) for t in sorted(TRUST_CHANGES)]
)
def test_trust_change_after_a_noop_hit_declines(twin: Twin, trust: str) -> None:
    """Manifest and lockfile bytes unchanged: only the trust input moved, so FULL decides."""
    _converge_exec_user_approved(twin)
    _assert_decision(twin.ok("install"), NOOP)
    pinned = {w.root: _snapshot(w) for w in twin.worlds}
    TRUST_CHANGES[trust](twin)
    for world in twin.worlds:
        assert _snapshot(world) == pinned[world.root], (
            "the trust change touched apm.yml or the lock"
        )
    _assert_decision(twin.run("install"), FULL)


# -- S4: FRESH install (no lockfile) on a warm machine ------------------------------


def _fresh_checkout(world: World) -> None:
    """A fresh clone of the project on a machine whose cache is warm: no lock, no deployed tree."""
    _remove_deployed(world)
    (world.project / "apm.lock.yaml").unlink()


@NEEDS_FRESH
def test_fresh_install_on_a_warm_cache_applies_the_diff(twin: Twin) -> None:
    """Exit, output, project bytes AND the written lockfile bytes equal the full pipeline."""
    _converge(twin, "p-skill:git")
    twin.each(_fresh_checkout)
    _assert_decision(twin.ok("install"), DIFF)


def _fresh_store_miss(world: World) -> None:
    _fresh_checkout(world)
    for root in _cache_roots(world):
        shutil.rmtree(root, ignore_errors=True)


def _fresh_tampered_store(world: World) -> None:
    _tamper_store(world)
    (world.project / "apm.lock.yaml").unlink()


def _fresh_under_stricter_policy(world: World) -> None:
    _fresh_checkout(world)
    use_policy(world, "exec-deny-all")


FRESH_DOUBTS: dict[str, tuple[tuple[str, ...], Callable[[World], None]]] = {
    # doubt: (packages converged on the warm machine, change before the fresh install)
    "store-miss": (("p-skill:git",), _fresh_store_miss),
    "store-entry-tampered": (("p-skill:git",), _fresh_tampered_store),
    "policy-on": (("p-skill:git",), _fresh_under_stricter_policy),
    "local-source": (("p-skill",), _fresh_checkout),
    "mcp-heavy-package": (BOTH, _fresh_checkout),
}


@pytest.mark.parametrize(
    "doubt", [pytest.param(d, marks=NEEDS_ANY.marks) for d in sorted(FRESH_DOUBTS)]
)
def test_fresh_install_doubt_declines_to_the_full_pipeline(twin: Twin, doubt: str) -> None:
    keys, change = FRESH_DOUBTS[doubt]
    _converge(twin, *keys)
    twin.each(change)
    _assert_decision(twin.ok("install"), FULL)


@NEEDS_ANY
def test_fresh_marketplace_install_declines_to_the_full_pipeline(twin: Twin) -> None:
    _marketplace_source(twin)
    twin.each(_fresh_checkout)
    _assert_decision(twin.ok("install"), FULL)
