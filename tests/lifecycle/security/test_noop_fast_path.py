"""No-op install fast path: a replayed no-op is the no-op the full pipeline prints.

PROMISE: P-INV-I2 -- repeating a converged install changes no bytes; and
P-INV-I4 -- a deny, revoked approval or stricter policy applied between two
runs is enforced by the next install even when apm.yml and apm.lock.yaml are
byte-identical (source: tests/lifecycle/oracles.py, src/apm_cli/install/
noop_fast_path.py module docstring).

Differential oracle: two clones of one world run the same steps, one with the
fast path on and one with ``APM_INSTALL_FAST_PATH=1`` and one with it off (the full pipeline).
Both converge (install, then two no-ops; the fast world proves its third run
replayed), then the same change is applied to both and ``apm install`` runs.
Exit code, output (paths and the elapsed time normalized) and the bytes of
the project and HOME must match. A fast path that skipped any input check
would replay ``No changes`` and keep the stale state, failing the cell.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Callable
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
    # PR CI proves the fall-through cells at every head (QL H8).
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise("P-INV-I2", "P-INV-I4"),
    # The fast path never engages on Windows (os.path.exists is not observable there).
    pytest.mark.skipif(sys.platform == "win32", reason="no-op replay is POSIX-only"),
]

_FAST_ENV = "APM_INSTALL_FAST_PATH"
_HIT = "[i] install fast path: replayed a verified no-op"
_ELAPSED = re.compile(r"\b\d+\.\ds\b")


def _declare(world: World, *keys: str) -> None:
    manifest = world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data.setdefault("dependencies", {})["apm"] = [world.packages[k].spec for k in keys]
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")


def _digest(root: Path, skip: tuple[str, ...] = (), world: World | None = None) -> dict[str, str]:
    """File digests under *root*; with *world*, its root path is normalized away."""
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if any(rel == s or rel.startswith(s + "/") for s in skip):
            continue
        if path.is_symlink():
            files[rel] = "link:" + str(path.readlink())
        elif path.is_file():
            data = path.read_bytes()
            if world is not None:
                data = data.replace(str(world.root).encode(), b"<ROOT>")
            files[rel] = hashlib.sha256(data).hexdigest()
    return files


# Caches (git objects, policy, stamps) are not install state; the stamp lives there.
_HOME_CACHES = (".cache", "Library/Caches", ".apm/logs")
# Per-checkout identity: a random installation id and receipts keyed by the
# project path differ between two worlds that deployed the same content.
_IDENTITY = (".apm-local", ".apm/deployment-receipts")


def _state(world: World) -> dict[str, dict[str, str]]:
    """Raw bytes, for no-write checks within one world."""
    return {
        "project": _digest(world.project, (".git",)),
        "home": _digest(world.home, _HOME_CACHES),
    }


def _comparable(world: World) -> dict[str, object]:
    """Install state comparable across two worlds: identity fields and paths normalized."""
    lock = yaml.safe_load((world.project / "apm.lock.yaml").read_text(encoding="utf-8"))
    return {
        "project": _digest(world.project, (".git", "apm.lock.yaml", *_IDENTITY), world),
        "home": _digest(world.home, _HOME_CACHES + _IDENTITY, world),
        "lock": _without_identity(lock, str(world.root)),
    }


def _without_identity(node: object, root: str) -> object:
    if isinstance(node, dict):
        return {
            k: _without_identity(v, root)
            for k, v in node.items()
            if not re.search(r"install(ation)?_?id|receipt|generated_at", str(k))
        }
    if isinstance(node, list):
        return [_without_identity(v, root) for v in node]
    return node.replace(root, "<ROOT>") if isinstance(node, str) else node


def _normalized(world: World, text: str) -> str:
    text = text.replace(str(world.root), "<ROOT>")
    return _ELAPSED.sub("<T>", text)


def _run(world: World, argv: tuple[str, ...], *, fast: str | None):
    world.env.pop(_FAST_ENV, None)
    if fast is not None:
        world.env[_FAST_ENV] = fast
    try:
        return driver.run(world, argv)
    finally:
        world.env.pop(_FAST_ENV, None)


def _converge(world: World, *, fast: bool) -> None:
    """Install, then three no-ops: pending marker, recording, and (fast world) a replay."""
    for attempt in range(4):
        trace = fast and attempt == 3
        mode = "trace" if trace else ("1" if fast else "0")
        result = _run(world, ("install",), fast=mode)
        assert result.returncode == 0, result.stdout + result.stderr
        if trace:
            assert _HIT in result.stderr, (
                "the converged fourth install must replay the recorded no-op:\n"
                + result.stdout
                + result.stderr
            )
            assert "No changes -- install state already up to date" in result.stdout


def _approve_exec(world: World) -> None:
    approved = driver.run(world, ("approve", f"{ORG}/p-exec"))
    assert approved.returncode == 0, approved.stdout + approved.stderr


def _first_deployed(world: World) -> Path:
    lock = LockFile.read(world.project / "apm.lock.yaml")
    assert lock is not None
    for dep in lock.dependencies.values():
        for rel in sorted(dep.deployed_file_hashes):
            path = world.project / rel
            if path.is_file():
                return path
    raise AssertionError("no hashed deployed file in the lock")


def _tamper(world: World) -> None:
    path = _first_deployed(world)
    path.write_bytes(path.read_bytes() + b"\nlocal edit\n")


def _delete(world: World) -> None:
    _first_deployed(world).unlink()


def _user_config(world: World) -> Path:
    return world.apm_home / "config.json"


def _revoke_user_approval(world: World) -> None:
    """Drop the user-scope grant of p-exec from ~/.apm/config.json (apm.yml untouched)."""
    config = _user_config(world)
    data = json.loads(config.read_text(encoding="utf-8"))
    ref = world.packages["p-exec:git"]
    identities = (ref.spec, ref.name, f"{ORG}/{ref.name}")
    allow = data.get("executables", {}).get("allow", {})
    dropped = [k for k in list(allow) if any(k == i or k.startswith(i + "#") for i in identities)]
    assert dropped, f"no user approval of p-exec to revoke in {allow}"
    for entry in dropped:
        del allow[entry]
    config.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _user_deny(world: World) -> None:
    denied = driver.run(world, ("deny", f"{ORG}/p-exec"), scope="user")
    assert denied.returncode == 0, denied.stdout + denied.stderr


def _policy(variant: str) -> Callable[[World], None]:
    return lambda world: use_policy(world, variant)


# change id -> (arrange both worlds, change applied between the runs)
CHANGES: dict[str, Callable[[World], None]] = {
    "tampered-deployed-file": _tamper,
    "deleted-deployed-file": _delete,
    "user-deny-after-approval": _user_deny,
    "revoked-user-approval": _revoke_user_approval,
    "policy-denies-dependency": _policy("deny-pkg"),
    "policy-denies-executables": _policy("exec-deny-all"),
}


def _arrange(world: World, change: str) -> None:
    use_policy(world, "allow")
    _declare(world, "p-exec:git", "p-skill:git")
    if change == "revoked-user-approval":
        # The grant lives only in the user store, so revoking it leaves apm.yml
        # and the lockfile byte-identical.
        approved = driver.run(world, ("approve", f"{ORG}/p-exec"), scope="user")
        assert approved.returncode == 0, approved.stdout + approved.stderr
    else:
        _approve_exec(world)


@pytest.mark.parametrize("change", sorted(CHANGES))
def test_install_after_change_matches_the_full_pipeline(
    lifecycle_template: World, tmp_path: Path, change: str
) -> None:
    worlds = {
        "fast": clone(lifecycle_template, tmp_path / "fast"),
        "full": clone(lifecycle_template, tmp_path / "full"),
    }
    for name, world in worlds.items():
        world.services.reset()
        _arrange(world, change)
        _converge(world, fast=name == "fast")
    manifests = {n: (w.project / "apm.yml").read_bytes() for n, w in worlds.items()}
    locks = {n: (w.project / "apm.lock.yaml").read_bytes() for n, w in worlds.items()}

    observed = {}
    for name, world in worlds.items():
        CHANGES[change](world)
        # Every change happens outside apm.yml and the lockfile (QL bar item 3).
        assert (world.project / "apm.yml").read_bytes() == manifests[name]
        assert (world.project / "apm.lock.yaml").read_bytes() == locks[name]
        result = _run(world, ("install",), fast="1" if name == "fast" else "0")
        observed[name] = (
            result.returncode,
            _normalized(world, result.stdout),
            _normalized(world, result.stderr),
            _comparable(world),
        )

    fast, full = observed["fast"], observed["full"]
    assert "No changes -- install state already up to date" not in full[1], (
        f"{change} must be a real change for the full pipeline:\n{full[1]}"
    )
    assert fast[0] == full[0], f"exit {fast[0]} != full pipeline {full[0]}\n{fast[1]}{fast[2]}"
    assert fast[1] == full[1], f"stdout differs from the full pipeline:\n{fast[1]}\n---\n{full[1]}"
    assert fast[2] == full[2], f"stderr differs from the full pipeline:\n{fast[2]}\n---\n{full[2]}"
    assert fast[3] == full[3], "installed state differs from the full pipeline"


def test_replayed_noop_writes_nothing_and_matches_the_full_noop(
    lifecycle_template: World, tmp_path: Path
) -> None:
    fast = clone(lifecycle_template, tmp_path / "fast")
    full = clone(lifecycle_template, tmp_path / "full")
    for name, world in (("fast", fast), ("full", full)):
        world.services.reset()
        _arrange(world, "none")
        _converge(world, fast=name == "fast")
    before = _state(fast)
    world_cache = json.dumps(sorted(_digest(fast.home).items()))
    fast.services.reset()
    replayed = _run(fast, ("install",), fast="1")
    reference = _run(full, ("install",), fast="0")
    assert replayed.returncode == reference.returncode == 0
    assert _normalized(fast, replayed.stdout) == _normalized(full, reference.stdout)
    assert _normalized(fast, replayed.stderr) == _normalized(full, reference.stderr)
    assert _state(fast) == before, "a replayed no-op must write nothing"
    assert json.dumps(sorted(_digest(fast.home).items())) == world_cache, (
        "a replayed no-op must not write HOME, caches included"
    )
    assert fast.services.egress_log() == [], "a replayed no-op must not reach the network"
