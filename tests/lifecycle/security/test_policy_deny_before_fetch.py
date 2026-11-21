"""A dependency a blocking org policy denies is never fetched.

policy-schema.md: org policy loads before any package is written to disk. A
deny (or allow) rule decides on the dependency identity alone, so a refused
dependency is never contacted, cloned or cached: not as a direct dependency,
not as a transitive one, and not on a frozen replay. The world proxy records
every request (egress oracle) and the cache tree is digested around each run.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from urllib.parse import unquote

import pytest
import yaml

from tests.lifecycle import driver
from tests.lifecycle.world import World
from tests.lifecycle.world.builder import TAG
from tests.lifecycle.world.scopes import use_policy
from tests.lifecycle.world.services import DENIED_PACKAGE, HOST_ALIASES, ORG

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.promise("P-OWN-policy-violation-subjects", "P-INV-I7"),
]

_DENIED = f"github.com/{ORG}/{DENIED_PACKAGE} -- denied by pattern: **/{DENIED_PACKAGE}"
_BLOCKED = "Install blocked by org policy"


def _cache_root(world: World) -> Path:
    return world.cache_root or world.root / "cache"


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()) if root.is_dir() else ():
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def _denied_requests(world: World) -> list[dict]:
    """Every request naming the denied repository, on any host or API shape."""
    return [
        entry
        for entry in world.services.egress_log()
        if DENIED_PACKAGE in unquote(str(entry["path"]))
    ]


def _cached_denied(world: World) -> list[str]:
    root = _cache_root(world)
    if not root.is_dir():
        return []
    return sorted(str(p) for p in root.rglob("*") if DENIED_PACKAGE in p.name)


def _declare(world: World, *specs: str) -> None:
    manifest = world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data.setdefault("dependencies", {})["apm"] = list(specs)
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")


def _project_state(world: World) -> dict[str, bytes]:
    return {
        path.relative_to(world.project).as_posix(): path.read_bytes()
        for path in sorted(world.project.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(world.project).parts
    }


def _assert_refused_unfetched(
    world: World, result, cache_before: str, state_before, *, denied: str = _DENIED
) -> None:
    output = " ".join((result.stdout + result.stderr).split())
    assert result.returncode == 1, output
    assert denied in output, output
    assert _BLOCKED in output, output
    assert _denied_requests(world) == [], _denied_requests(world)
    assert _cached_denied(world) == [], _cached_denied(world)
    assert _tree_digest(_cache_root(world)) == cache_before
    assert _project_state(world) == state_before


@pytest.mark.parametrize("form", ["manifest", "spec"])
def test_direct_denied_dependency_is_never_fetched(lifecycle_world: World, form: str) -> None:
    world = lifecycle_world
    use_policy(world, "deny-pkg")
    spec = world.packages[f"{DENIED_PACKAGE}:git"].spec
    argv = ["install"]
    if form == "manifest":
        _declare(world, spec)
    else:
        argv.append(spec)
    # The policy itself is fetched and cached first, so the cache digest
    # isolates package fetches.
    driver.run(world, ["policy", "status"])
    cache_before = _tree_digest(_cache_root(world))
    state_before = _project_state(world)
    world.services.reset()

    result = driver.run(world, argv)

    _assert_refused_unfetched(world, result, cache_before, state_before)


def _publish_wrapper(world: World) -> str:
    """Serve an allowed package whose only dependency is the denied one."""
    tree = world.root / "wrapper-src"
    tree.mkdir()
    manifest = {
        "name": "p-wrapper",
        "version": "1.0.0",
        "dependencies": {"apm": [f"{ORG}/{DENIED_PACKAGE}#{TAG}"]},
    }
    (tree / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    skill = tree / ".apm" / "skills" / "p-wrapper"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: p-wrapper\ndescription: wrapper\n---\n\nWrapper.\n", encoding="utf-8"
    )
    bare = world.root / "wrapper.git"
    git = ("git", "-c", "user.name=lifecycle", "-c", "user.email=lifecycle@example.invalid")
    for args in (
        ("-C", str(tree), "init", "-q", "--initial-branch=main"),
        ("-C", str(tree), "add", "-A"),
        ("-C", str(tree), "commit", "-qm", "wrapper"),
        ("-C", str(tree), "tag", TAG),
        ("clone", "-q", "--bare", str(tree), str(bare)),
    ):
        subprocess.run((*git, *args), env=world.env, check=True, capture_output=True)
    world.services.serve(f"{ORG}/p-wrapper", bare)
    return f"{ORG}/p-wrapper#{TAG}"


def test_transitive_denied_dependency_is_never_fetched(lifecycle_world: World) -> None:
    world = lifecycle_world
    use_policy(world, "deny-pkg")
    _declare(world, _publish_wrapper(world))
    driver.run(world, ["policy", "status"])
    state_before = _project_state(world)
    world.services.reset()

    result = driver.run(world, ["install"])

    output = " ".join((result.stdout + result.stderr).split())
    assert result.returncode == 1, output
    assert _DENIED in output, output
    assert _BLOCKED in output, output
    # The allowed parent may be fetched; the denied child never is.
    assert _denied_requests(world) == [], _denied_requests(world)
    assert _cached_denied(world) == [], _cached_denied(world)
    assert _project_state(world) == state_before


def test_frozen_and_audit_refuse_a_locked_denied_dependency_alike(lifecycle_world: World) -> None:
    world = lifecycle_world
    use_policy(world, "allow")
    _declare(world, world.packages[f"{DENIED_PACKAGE}:git"].spec)
    installed = driver.run(world, ["install"])
    assert installed.returncode == 0, installed.stdout + installed.stderr
    use_policy(world, "deny-pkg")
    driver.run(world, ["policy", "status"])
    state_before = _project_state(world)
    world.services.reset()

    frozen = driver.run(world, ["install", "--frozen"])
    audit = driver.run(world, ["audit", "--ci"])

    for result in (frozen, audit):
        output = " ".join((result.stdout + result.stderr).split())
        assert result.returncode == 1, output
        assert f"{ORG}/{DENIED_PACKAGE}" in output, output
    assert _denied_requests(world) == [], _denied_requests(world)
    assert _project_state(world) == state_before


def _run_after_snapshot(world: World, argv: list[str]):
    """Snapshot the cache and project, reset egress, then run *argv*."""
    driver.run(world, ["policy", "status"])
    cache_before = _tree_digest(_cache_root(world))
    state_before = _project_state(world)
    world.services.reset()
    return driver.run(world, argv), cache_before, state_before


def test_denied_gitlab_shorthand_is_never_probed(lifecycle_world: World) -> None:
    """The GitLab host/path boundary probe never reaches a denied repository."""
    world = lifecycle_world
    use_policy(world, "deny-pkg")
    host, prefix = HOST_ALIASES["gitlab"]
    spec = f"{host}/{prefix}/{DENIED_PACKAGE}#{TAG}"
    result, cache_before, state_before = _run_after_snapshot(world, ["install", spec])
    assert result.returncode == 1, result.stdout + result.stderr
    denied = f"{host}/{prefix}/{DENIED_PACKAGE} -- denied by pattern: **/{DENIED_PACKAGE}"
    _assert_refused_unfetched(world, result, cache_before, state_before, denied=denied)


def test_marketplace_entry_resolving_to_a_denied_repo_is_never_fetched(
    lifecycle_world: World,
) -> None:
    """The catalog may be read; the denied repository it names is never fetched."""
    world = lifecycle_world
    use_policy(world, "deny-pkg")
    added = driver.run(world, ["marketplace", "add", "lifecycle/marketplace"])
    assert added.returncode == 0, added.stdout + added.stderr
    spec = world.packages[f"{DENIED_PACKAGE}:marketplace"].spec
    result, cache_before, state_before = _run_after_snapshot(world, ["install", spec])
    assert result.returncode == 1, result.stdout + result.stderr
    _assert_refused_unfetched(world, result, cache_before, state_before)
