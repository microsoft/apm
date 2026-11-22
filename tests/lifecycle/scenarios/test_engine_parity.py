"""Install engine parity: the fast engine and the full pipeline leave identical state.

PROMISE: P-INV-I2 -- repeating a converged install changes no bytes, whichever
engine answers (source: tests/lifecycle/oracles.py).
PROMISE: P-INV-I1 -- the lockfile matches the deployed files after every step.
PROMISE: P-INV-I4 -- a stricter policy, an edited or deleted deployed file, a
changed manifest, a lockfile from another apm version, a store miss or a
non-default flag is honored by the next install: the fast engine declines and
the full pipeline decides ("any doubt = full pipeline", Lane C plan).

DIFFERENTIAL ORACLE. Two clones of one world run the same steps: the fast
world with ``APM_INSTALL_FAST_PATH=trace`` (one decision line on stderr per
install), the full world with ``APM_INSTALL_FAST_PATH=0`` (every fast engine
off). After every step the exit code, the output (paths, timings and the
decision line normalized away) and the bytes of the project, HOME and the
lockfile (identity fields aside) must match.

DECISIONS (engine contract agreed with the no-op and diff-apply owners):
``no-op (...)``, ``applied a verified diff (<n> files)`` or ``full run
(<reason>)``. A cell names the decision it requires, so it is red while the
engine owning that decision is absent: it is ``xfail(strict=True)`` until the
engine module exists, then it must pass.
"""

from __future__ import annotations

import hashlib
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
from tests.lifecycle.world import World
from tests.lifecycle.world.clone import clone
from tests.lifecycle.world.scopes import use_policy
from tests.lifecycle.world.services import ORG

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
BOTH = ("p-skill:git", "p-exec:git")


def _present(*modules: str) -> bool:
    return any(importlib.util.find_spec(name) is not None for name in modules)


NOOP_ENGINE = _present("apm_cli.install.noop_diff", "apm_cli.install.desired_state")
DIFF_ENGINE = _present("apm_cli.install.diff_apply")


class MissingDecision(AssertionError):
    """The fast world did not take the decision the cell requires."""


def _owned_by(present: bool, engine: str) -> pytest.MarkDecorator:
    """Red-first: strict xfail while *engine* is absent; required once it lands.

    Only a missing decision is the expected failure: a parity break or a failed
    command fails the cell even at main.
    """
    return pytest.mark.xfail(
        not present,
        strict=True,
        raises=MissingDecision,
        reason=f"{engine} engine absent (red-first cell)",
    )


def _kind(decision: str) -> str:
    if decision.startswith("no-op"):
        return NOOP
    if decision.startswith("applied a verified diff"):
        return DIFF
    if decision.startswith("full run"):
        return FULL
    return f"none ({decision!r})"


# -- comparable state -----------------------------------------------------------

# Caches and logs are not install state; per-checkout identity differs by design.
_HOME_SKIP = (".cache", "Library/Caches", ".apm/logs", ".apm-local", ".apm/deployment-receipts")
_PROJECT_SKIP = (".git", "apm.lock.yaml", ".apm-local", ".apm/deployment-receipts")
_IDENTITY_KEY = re.compile(r"install(ation)?_?id|receipt|generated_at")


def _digest(root: Path, skip: tuple[str, ...], world: World) -> dict[str, str]:
    marker = str(world.root)
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")) if root.exists() else ():
        rel = path.relative_to(root).as_posix()
        if any(rel == s or rel.startswith(s + "/") for s in skip):
            continue
        if path.is_symlink():
            files[rel] = "link:" + str(path.readlink()).replace(marker, "<ROOT>")
        elif path.is_file():
            data = path.read_bytes().replace(marker.encode(), b"<ROOT>")
            mode = "x" if path.stat().st_mode & 0o111 else "-"
            files[rel] = mode + hashlib.sha256(data).hexdigest()
    return files


def _without_identity(node: object, marker: str) -> object:
    if isinstance(node, dict):
        return {
            k: _without_identity(v, marker)
            for k, v in node.items()
            if not _IDENTITY_KEY.search(str(k))
        }
    if isinstance(node, list):
        return [_without_identity(v, marker) for v in node]
    return node.replace(marker, "<ROOT>") if isinstance(node, str) else node


def _state(world: World) -> dict[str, object]:
    lock_path = world.project / "apm.lock.yaml"
    lock = yaml.safe_load(lock_path.read_text(encoding="utf-8")) if lock_path.exists() else None
    return {
        "project": _digest(world.project, _PROJECT_SKIP, world),
        "home": _digest(world.home, _HOME_SKIP, world),
        "lock": _without_identity(lock, str(world.root)),
    }


def _normalized(world: World, text: str) -> list[str]:
    text = _ELAPSED.sub("<T>", _DECISION.sub("", text).replace(str(world.root), "<ROOT>"))
    return [line for line in text.splitlines() if line.strip()]


# -- the twin -------------------------------------------------------------------


@dataclass
class Twin:
    fast: World
    full: World
    codes: list[int] = field(default_factory=list)

    @property
    def worlds(self) -> tuple[World, World]:
        return (self.fast, self.full)

    def each(self, change: Callable[[World], None]) -> None:
        for world in self.worlds:
            change(world)

    def run(self, *argv: str) -> str:
        """Run *argv* in both worlds, assert parity, return the fast world's decision."""
        observed = []
        decision = ""
        for world, mode in ((self.fast, "trace"), (self.full, "0")):
            world.env[FAST_ENV] = mode
            try:
                result = driver.run(world, argv)
            finally:
                world.env.pop(FAST_ENV, None)
            if world is self.fast:
                lines = _DECISION.findall(result.stderr)
                assert len(lines) <= 1, f"one decision per command, got {lines}"
                decision = lines[0] if lines else ""
            observed.append(
                (
                    result.returncode,
                    _normalized(world, result.stdout),
                    _normalized(world, result.stderr),
                    _state(world),
                )
            )
        (code, out, err, state), (ref_code, ref_out, ref_err, ref_state) = observed
        label = " ".join(argv)
        assert code == ref_code, f"{label}: exit {code} != full pipeline {ref_code}\n{out}\n{err}"
        assert out == ref_out, f"{label}: stdout differs from the full pipeline"
        assert err == ref_err, f"{label}: stderr differs from the full pipeline"
        assert state == ref_state, f"{label}: installed state differs from the full pipeline"
        self.codes.append(code)
        return decision

    def ok(self, *argv: str) -> str:
        decision = self.run(*argv)
        assert self.codes[-1] == 0, f"{' '.join(argv)} failed in both worlds"
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


def _deployed(world: World) -> list[Path]:
    lock = LockFile.read(world.project / "apm.lock.yaml")
    assert lock is not None, "no lockfile"
    paths = [
        world.project / rel
        for dep in lock.dependencies.values()
        for rel in sorted(dep.deployed_file_hashes)
        if (world.project / rel).is_file()
    ]
    assert paths, "the lock records no hashed deployed file"
    return paths


def _assert_decision(decision: str, *kinds: str) -> None:
    if _kind(decision) not in kinds:
        raise MissingDecision(f"expected a {'/'.join(kinds)} decision, got {decision!r}")


def _publish_skill(twin: Twin, body: str, tag: str) -> None:
    source = Path(twin.fast.packages["p-skill"].spec)
    skill = next(source.rglob("SKILL.md")).relative_to(source).as_posix()
    text = f"---\nname: p-skill\ndescription: {body}\n---\n\n{body}\n"
    twin.fast.services.publish_revision(f"{ORG}/p-skill", {skill: text}, tag=tag)


def _remove_deployed(world: World) -> None:
    for path in _deployed(world):
        path.unlink()
    shutil.rmtree(world.project / "apm_modules", ignore_errors=True)


# -- journeys the engines claim -------------------------------------------------


@_owned_by(NOOP_ENGINE, "no-op")
def test_reinstall_of_a_converged_project_is_a_noop(twin: Twin) -> None:
    _converge(twin, *BOTH)
    _assert_decision(twin.ok("install"), NOOP)
    _assert_decision(twin.ok("install"), NOOP)


@_owned_by(NOOP_ENGINE, "no-op")
def test_frozen_reinstall_of_a_converged_project_is_a_noop(twin: Twin) -> None:
    _converge(twin, *BOTH)
    _assert_decision(twin.ok("install", "--frozen"), NOOP)


@_owned_by(NOOP_ENGINE, "no-op")
def test_install_after_uninstall_matches_the_full_pipeline(twin: Twin) -> None:
    _converge(twin, *BOTH)
    twin.ok("uninstall", f"{ORG}/p-skill")
    _assert_decision(twin.ok("install"), NOOP, FULL)


@_owned_by(DIFF_ENGINE, "diff-apply")
def test_warm_locked_install_applies_the_diff(twin: Twin) -> None:
    """Lockfile and store present, deployed tree gone: a fresh checkout on a warm machine."""
    _converge(twin, *BOTH)
    twin.each(_remove_deployed)
    _assert_decision(twin.ok("install"), DIFF)


@_owned_by(DIFF_ENGINE, "diff-apply")
def test_frozen_ci_on_a_warm_store_applies_the_diff(twin: Twin) -> None:
    _converge(twin, *BOTH)
    twin.each(_remove_deployed)
    _assert_decision(twin.ok("install", "--frozen"), DIFF)


@_owned_by(DIFF_ENGINE, "diff-apply")
def test_branch_switch_lockfile_swap_applies_the_diff(twin: Twin) -> None:
    """Install v1, move to v2, then check out v1's manifest and lockfile again."""
    _converge(twin, "p-skill:git")
    saved = {
        w.root: ((w.project / "apm.yml").read_bytes(), (w.project / "apm.lock.yaml").read_bytes())
        for w in twin.worlds
    }
    _publish_skill(twin, "Version two.", tag="v2.0.0")

    def to_v2(world: World) -> None:
        manifest = world.project / "apm.yml"
        text = manifest.read_text(encoding="utf-8")
        manifest.write_text(text.replace("p-skill#v1.0.0", "p-skill#v2.0.0"), encoding="utf-8")

    twin.each(to_v2)
    twin.ok("install")

    def back_to_v1(world: World) -> None:
        manifest, lock = saved[world.root]
        (world.project / "apm.yml").write_bytes(manifest)
        (world.project / "apm.lock.yaml").write_bytes(lock)

    twin.each(back_to_v1)
    _assert_decision(twin.ok("install"), DIFF)


@_owned_by(NOOP_ENGINE, "no-op")
def test_deleted_deployed_file_is_restored_like_the_full_pipeline(twin: Twin) -> None:
    """The engine may restore it from the store or decline; the bytes must match either way."""
    _converge(twin, *BOTH)
    twin.each(lambda w: _deployed(w)[0].unlink())
    _assert_decision(twin.ok("install"), DIFF, FULL)


def test_update_matches_the_full_pipeline(twin: Twin) -> None:
    """No engine claims ``update``: parity guard, green at main, must stay green."""
    _converge(twin, "p-skill:git")
    _publish_skill(twin, "Moved tag.", tag="v1.0.0")
    _assert_decision(twin.ok("update", "--yes") or "full run (not install)", FULL)


# -- doubt triggers: the fast engine must decline ---------------------------------


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


def _empty_store(world: World) -> None:
    _remove_deployed(world)
    for root in (world.cache_root, world.env.get("APM_CACHE_DIR")):
        if root:
            shutil.rmtree(root, ignore_errors=True)


def _nothing(world: World) -> None:
    del world


DOUBTS: dict[str, tuple[Callable[[World], None], tuple[str, ...], bool, str]] = {
    # trigger: (change after converging, install argv, owner present, owner)
    "apm-version-changed": (_lock_from_another_apm, ("install",), NOOP_ENGINE, "no-op"),
    "apm-yml-changed": (_drop_exec_from_manifest, ("install",), NOOP_ENGINE, "no-op"),
    "deployed-file-edited": (_edit_deployed, ("install",), NOOP_ENGINE, "no-op"),
    "non-default-flag": (_nothing, ("install", "--force"), NOOP_ENGINE, "no-op"),
    "policy-changed": (_stricter_policy, ("install",), NOOP_ENGINE, "no-op"),
    "store-miss": (_empty_store, ("install",), DIFF_ENGINE, "diff-apply"),
}


@pytest.mark.parametrize(
    "trigger",
    [pytest.param(t, marks=_owned_by(DOUBTS[t][2], DOUBTS[t][3])) for t in sorted(DOUBTS)],
)
def test_doubt_trigger_declines_to_the_full_pipeline(twin: Twin, trigger: str) -> None:
    change, argv, _, _ = DOUBTS[trigger]
    _converge(twin, *BOTH)
    twin.each(change)
    _assert_decision(twin.run(*argv), FULL)


@_owned_by(NOOP_ENGINE, "no-op")
def test_hidden_unicode_finding_declines_to_the_full_pipeline(twin: Twin) -> None:
    """A scanner WARNING in a package copy (mid-file zero-width chars) is re-reported in full."""
    _publish_skill(twin, "Use p\u200bskill.\ufeff Mid-file BOM.", tag="v1.0.0")
    _converge(twin, "p-skill:git")
    _assert_decision(twin.ok("install"), FULL)


@_owned_by(NOOP_ENGINE, "no-op")
def test_external_scanners_flag_declines_to_the_full_pipeline(twin: Twin) -> None:
    _converge(twin, *BOTH)
    twin.ok("experimental", "enable", "external_scanners")
    _assert_decision(twin.run("install"), FULL)


@_owned_by(NOOP_ENGINE, "no-op")
def test_local_path_source_declines_to_the_full_pipeline(twin: Twin) -> None:
    """A local directory has no immutable pin: the engine cannot prove it unchanged."""
    _converge(twin, "p-skill")
    _assert_decision(twin.ok("install"), FULL)
