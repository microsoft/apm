"""Shared install engine parity harness: the fast engine and the full pipeline leave identical state.

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

import hashlib
import importlib.util
import os
import re
import shutil
import signal
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

from apm_cli.deps.lockfile import LockFile
from tests.lifecycle import driver
from tests.lifecycle.known import KnownFailure
from tests.lifecycle.scenarios._deploy_diff import (
    HOME_CACHES,
    comparable,
    diff,
    revoke_user_approval,
    rooted,
)
from tests.lifecycle.world import World
from tests.lifecycle.world.clone import clone
from tests.lifecycle.world.packages import MCP_SERVER
from tests.lifecycle.world.scopes import use_policy, use_registry
from tests.lifecycle.world.services import (
    ORG,
    POLICY_FILE,
    POLICY_REPO,
    policy_documents,
    policy_org,
)
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.lifecycle_evidence import expect_output_violation

FAST_ENV = "APM_INSTALL_FAST_PATH"


NOOP = "no-op"


DIFF = "diff"


FULL = "full"


_DECISION = re.compile(r"^\[i\] install fast path: (.*)$", re.MULTILINE)


_ELAPSED = re.compile(r"\b\d+(?:\.\d+)?m?s\b")


_PARALLEL = re.compile(r"^\s*\[>\] Resolving ")


# CommandLogger.policy_resolved prints the age in s, m or h (command_logger.py).
_POLICY_AGE = re.compile(r" \(cached, fetched \d+[smh] ago\)")


BOTH = ("p-skill:git", "p-exec:git")


DEPLOY_BUCKET = "deploy_v1"


POLICY_CACHE = "policy_v1"  # policy/discovery.py POLICY_CACHE_DIR


def _present(*modules: str) -> bool:
    return any(importlib.util.find_spec(name) is not None for name in modules)


NOOP_ENGINE = _present("apm_cli.install.noop_diff", "apm_cli.install.desired_state")


DIFF_ENGINE = _present("apm_cli.install.diff_apply")


FRESH_ENGINE = _present("apm_cli.install.fresh_apply")


ANY_ENGINE = NOOP_ENGINE or DIFF_ENGINE


class MissingDecision(AssertionError):
    """The fast world did not take the decision the cell requires."""


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
    text = rooted(_POLICY_AGE.sub("", _DECISION.sub("", text)), str(world.root))
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


_RECEIPTS = "deployment-receipts"


def _modes(world: World) -> dict[str, str]:
    """Full permission bits of project, HOME and APM_HOME paths (apm_modules included).

    Caches, logs and checkout identity are skipped; checkout-keyed receipt file names
    are normalized, so each receipt directory compares as the sorted list of its modes.
    """
    modes: dict[str, str] = {}
    receipts: dict[str, list[str]] = {}
    roots = {"project": world.project, "home": world.home, "apm_home": world.apm_home}
    skips = {
        "project": (".git", ".apm-local"),
        "home": (*HOME_CACHES, ".apm/cache"),
        "apm_home": ("cache", "logs"),
    }
    for name, root in roots.items():
        skip = skips[name]
        for path in sorted(root.rglob("*")) if root.exists() else ():
            rel = path.relative_to(root).as_posix()
            if any(rel == s or rel.startswith(s + "/") for s in skip):
                continue
            mode = oct(path.lstat().st_mode & 0o7777)
            parts = rel.split("/")
            if _RECEIPTS in parts[:-1] and path.is_file():
                # Its own key: the receipt directory's mode stays compared under rel.
                key = f"{name}:{path.parent.relative_to(root).as_posix()}/<receipt files>"
                receipts.setdefault(key, []).append(mode)
            else:
                modes[f"{name}:{rel}"] = mode
    modes.update({key: ",".join(sorted(values)) for key, values in receipts.items()})
    return modes


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
            comparable(self.fast, exempt, receipt_history=True),
            comparable(self.full, exempt, receipt_history=True),
            self.worlds,
            ("fast", "full"),
        )
        assert not problems, f"{label}: state differs from the full pipeline\n" + "\n".join(
            problems
        )
        modes = [_modes(world) for world in self.worlds]
        assert modes[0] == modes[1], f"{label}: file modes differ: " + ", ".join(
            f"{rel} fast={modes[0].get(rel)} full={modes[1].get(rel)}"
            for rel in sorted(modes[0].keys() | modes[1].keys())
            if modes[0].get(rel) != modes[1].get(rel)
        )
        self.codes.append(fast.returncode)
        self.evidence = evidence
        return decisions[0] if decisions else ""

    def ok(self, *argv: str) -> str:
        decision = self.run(*argv)
        assert self.codes[-1] == 0, f"{' '.join(argv)} failed in both worlds{self.evidence}"
        return decision


def make_twin(template: World, tmp_path: Path) -> Twin:
    """Two clones of *template*: the fast world and the full-pipeline world."""
    pair = Twin(clone(template, tmp_path / "f"), clone(template, tmp_path / "r"))
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


def _clear_readonly(function, path, _exc) -> None:
    """Windows refuses to delete read-only files (git pack objects): clear the bit, retry."""
    os.chmod(path, stat.S_IWRITE)
    function(path)


def _rmtree(path: Path) -> None:
    """Remove *path* completely, or fail: a partial delete would differ between twins."""
    if os.path.lexists(path):
        shutil.rmtree(path, onexc=_clear_readonly)


def _remove_deployed(world: World) -> None:
    for path in _deployed(world):
        path.unlink()
    _rmtree(world.project / "apm_modules")


def _checkout(world: World, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        (world.project / rel).write_bytes(data)


def _snapshot(world: World) -> dict[str, bytes]:
    return {rel: (world.project / rel).read_bytes() for rel in ("apm.yml", "apm.lock.yaml")}


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


CHANGE_EXIT: dict[str, int] = {"symlinked-destination": 1}  # refuses to write through it


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
        _rmtree(root)


def _tamper_store(world: World) -> None:
    """Corrupt every deploy-store entry, then remove the deployed tree."""
    _remove_deployed(world)
    _tamper_store_entries(world)


def _tamper_store_entries(world: World) -> None:
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


def _clear_policy_cache(world: World) -> None:
    for root in _cache_roots(world):
        _rmtree(root / POLICY_CACHE)


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


def _fresh_checkout(world: World) -> None:
    """A fresh clone of the project on a machine whose cache is warm.

    No lock, no deployed tree and no checkout ownership evidence (the
    installation identity and its receipts); the machine cache stays warm.
    """
    _remove_deployed(world)
    (world.project / "apm.lock.yaml").unlink()
    _rmtree(world.project / ".apm-local")
    for folder in {
        world.home / ".apm" / "deployment-receipts",
        world.apm_home / "deployment-receipts",
    }:
        _rmtree(folder)


def _fresh_store_miss(world: World) -> None:
    _fresh_checkout(world)
    for root in _cache_roots(world):
        _rmtree(root)


def _fresh_tampered_store(world: World) -> None:
    _fresh_checkout(world)
    _tamper_store_entries(world)


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


DECLINES: dict[str, tuple[tuple[str, ...], Callable[[World], None], tuple[str, ...], int]] = {
    **{f"lock-{t}": (BOTH, change, argv, code) for t, (change, argv, code) in DOUBTS.items()},
    **{f"fresh-{d}": (keys, change, ("install",), 0) for d, (keys, change) in FRESH_DOUBTS.items()},
}


_RAW_SKIP = (".git", ".apm/logs")


def _raw_state(world: World) -> dict[str, dict[str, str]]:
    """Unnormalized bytes of everything an install may write: project, HOME, APM_HOME and
    caches, ownership metadata (.apm-local, receipts) and deploy-store entries included.
    """
    roots = {"project": world.project, "home": world.home, "apm_home": world.apm_home}
    roots.update({f"cache:{root}": root for root in _cache_roots(world)})
    return {name: _raw_digest(root) for name, root in roots.items()}


def _raw_digest(root: Path) -> dict[str, str]:
    """No normalization: raw bytes, raw symlink targets, directories and full lstat modes."""
    entries: dict[str, str] = {}
    for path in sorted(root.rglob("*")) if root.exists() else ():
        rel = path.relative_to(root).as_posix()
        if any(rel == s or rel.startswith(s + "/") for s in _RAW_SKIP):
            continue
        mode = oct(path.lstat().st_mode)
        if path.is_symlink():
            entries[rel] = f"{mode}:link:{path.readlink()}"
        elif path.is_dir():
            entries[rel] = f"{mode}:dir"
        else:
            entries[rel] = f"{mode}:" + hashlib.sha256(path.read_bytes()).hexdigest()
    return entries


DECIDE_EXIT = 3


def _run_outside_contract(world: World, argv: tuple[str, ...], env: dict[str, str], why: str):
    """Run *argv* through the driver's entry point with I8 suspended for this run only.

    For test-only exits outside every command's user exit contract (the decide seam,
    a killed process); the calling cell asserts the exit itself.
    """
    args = driver.command_args(argv, scope="project")
    runner = ApmLifecycleRunner(driver.driver_command(), timeout_seconds=driver.TIMEOUT_SECONDS)
    cwd = driver.command_cwd(world, "project")
    with expect_output_violation("I8", reason=why):
        return runner.run(args, cwd=cwd, env={**world.env, **env}, scenario_id="lifecycle-seam")


def _decide(world: World, argv: tuple[str, ...]):
    """Run *argv* in decide mode; this cell asserts the exit, ASCII and stdout itself."""
    why = f"the decide seam exits DECIDE_EXIT={DECIDE_EXIT} by design; the cell asserts it"
    return _run_outside_contract(world, argv, {FAST_ENV: "decide"}, why)


def _cache_stats(world: World) -> dict[str, str]:
    """``(size, mtime_ns)`` of every cache path, directories and roots included.

    Content digests miss a utime, an eviction-and-refill or a created root; the
    stat view sees them (cache_root, APM_CACHE_DIR and the APM_HOME cache).
    """
    roots = {*(_cache_roots(world)), world.apm_home / "cache"}
    stats: dict[str, str] = {}
    for root in sorted(roots):
        if not root.exists():
            stats[str(root)] = "<absent>"
            continue
        for path in [root, *sorted(root.rglob("*"))]:
            info = path.lstat()
            size = info.st_size if path.is_file() else "-"
            stats[str(path)] = f"{size}:{info.st_mtime_ns}:{oct(info.st_mode & 0o7777)}"
    return stats


@contextmanager
def _read_only_caches(world: World) -> Iterator[None]:
    """Drop every write bit on the cache trees for the block, then restore the modes."""
    roots = [r for r in {*(_cache_roots(world)), world.apm_home / "cache"} if r.exists()]
    saved = {
        path: path.lstat().st_mode & 0o7777
        for root in roots
        for path in [root, *root.rglob("*")]
        if not path.is_symlink()
    }
    try:
        for path, mode in saved.items():
            path.chmod(mode & ~0o222)
        yield
    finally:
        for path, mode in saved.items():
            path.chmod(mode)


def _assert_caches_untouched(before: dict[str, str], after: dict[str, str], label: str) -> None:
    moved = [p for p in sorted(before.keys() | after.keys()) if before.get(p) != after.get(p)]
    assert not moved, f"{label}: decide mode touched the caches: {moved}"


DECIDE_HITS = {
    "noop": (BOTH, _nothing, NOOP, NEEDS_NOOP.marks),
    "warm-locked": (BOTH, _remove_deployed, DIFF, NEEDS_DIFF.marks),
}


_WRITE_FAULT = """import os

_at = int(os.environ.get("APM_TEST_WRITE_FAULT_AT", "0"))
if _at:
    try:
        import apm_cli.install.diff_apply as _engine
    except Exception:
        _engine = None
    if _engine is not None and hasattr(_engine, "_write"):
        _original, _calls = _engine._write, [0]

        def _write(*args, **kwargs):
            _calls[0] += 1
            if _calls[0] == _at:
                raise OSError("injected write fault")
            return _original(*args, **kwargs)

        _engine._write = _write
"""


def _install_hook(world: World, source: str) -> None:
    """Put a ``sitecustomize`` running *source* first on *world*'s PYTHONPATH.

    Python loads only the first sitecustomize, so the world's network guard runs first.
    """
    hook = world.root / "fault-hook"
    hook.mkdir()
    existing = world.env.get("PYTHONPATH", "")
    guards = [
        Path(entry) / "sitecustomize.py"
        for entry in existing.split(os.pathsep)
        if entry and (Path(entry) / "sitecustomize.py").is_file()
    ]
    assert guards, "the world's network guard sitecustomize is missing"
    guard = guards[0].read_text(encoding="utf-8")
    (hook / "sitecustomize.py").write_text(guard + "\n\n" + source, encoding="utf-8")
    world.env["PYTHONPATH"] = os.pathsep.join(p for p in (str(hook), existing) if p)


def _lifecycle_scripts(world: World, tier: str) -> None:
    manifest = (world.project if tier == "project" else world.apm_home) / "apm.yml"
    exists = manifest.is_file()
    data = yaml.safe_load(manifest.read_text(encoding="utf-8")) if exists else None
    data = data or {"name": "user-scope", "version": "0.0.0"}
    log = f"echo {tier} >> lifecycle-{tier}.log"
    data["lifecycle"] = {
        "pre-install": [{"type": "command", "command": log}],
        "post-install": [{"type": "command", "command": log}],
    }
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


_KILL = """import os
import signal

_spec = os.environ.get("APM_TEST_KILL_AT", "")
if _spec:
    _module, _attrs, _nth, _when = _spec.split(":")
    try:
        import importlib

        _engine = importlib.import_module(_module)
    except Exception:
        _engine = None
    _names = [n for n in _attrs.split("|") if _engine is not None and hasattr(_engine, n)]
    if _names:
        _attr = _names[0]
        _original, _calls = getattr(_engine, _attr), [0]

        def _killing(*args, **kwargs):
            _calls[0] += 1
            hit = _calls[0] == int(_nth)
            if hit and _when == "before":
                os.kill(os.getpid(), signal.SIGKILL)
            result = _original(*args, **kwargs)
            if hit and _when == "after":
                os.kill(os.getpid(), signal.SIGKILL)
            return result

        setattr(_engine, _attr, _killing)

# Crash inside service reconciliation, after its first durable write: count the
# write seam's records while run_services runs and die before the Nth one.
_svc = int(os.environ.get("APM_TEST_KILL_IN_SERVICES", "0"))
if _svc:
    try:
        import apm_cli.install.fast_output as _output
        import apm_cli.integration.write_seam as _seam
    except Exception:
        _output = _seam = None
    if _output is not None and hasattr(_output, "run_services"):
        _state = {"active": False, "writes": 0}
        _run, _record = _output.run_services, _seam.record_before_write

        def run_services(*args, **kwargs):
            _state["active"] = True
            try:
                return _run(*args, **kwargs)
            finally:
                _state["active"] = False

        def record_before_write(path, *args, **kwargs):
            if _state["active"]:
                _state["writes"] += 1
                if _state["writes"] == _svc:
                    os.kill(os.getpid(), signal.SIGKILL)
            return _record(path, *args, **kwargs)

        _output.run_services = run_services
        _seam.record_before_write = record_before_write
"""


_ENGINE_MODULE = "apm_cli.install.diff_apply"


KILL_POINTS = {
    "after-first-package-published": {
        "APM_TEST_KILL_AT": f"{_ENGINE_MODULE}:_publish|_commit_package:1:after"
    },
    "after-first-file-linked": {"APM_TEST_KILL_AT": f"{_ENGINE_MODULE}:_write:1:after"},
    "before-services-render": {"APM_TEST_KILL_AT": f"{_ENGINE_MODULE}:_render:1:before"},
    # Before the 2nd service write: the 1st is durable, reconciliation unfinished.
    "after-first-service-write": {"APM_TEST_KILL_IN_SERVICES": "2"},
}


def _mcp_configs(world: World) -> list[Path]:
    """Project files that register the world's MCP server (service-owned configs)."""
    return [
        path
        for path in sorted(world.project.rglob("*.json"))
        if not path.relative_to(world.project).as_posix().startswith(("apm_modules/", ".git/"))
        and MCP_SERVER in path.read_text(encoding="utf-8", errors="replace")
    ]


_SIGKILL = getattr(signal, "SIGKILL", 9)  # absent on Windows, where the module is skipped


_KILLED = (-_SIGKILL, 128 + _SIGKILL)


_RELEASE_FAULT = """
try:
    import apm_cli.install.fast_output as _held_module
except Exception:
    _held_module = None
if _held_module is not None and hasattr(_held_module, "HeldOutput"):

    def _broken_release(self):
        raise OSError(32, "injected broken pipe while releasing held output")

    _held_module.HeldOutput.release = _broken_release
"""
