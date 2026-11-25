"""A monorepo lockfile does not depend on the checkout location (#5418).

PROMISE: P-LAW-portability.byte_deterministic -- two clones of one repository
at different paths write a byte-identical apm.lock.yaml: a local package
inside the repository but outside the project root (a monorepo sibling) is
recorded relative to the project root with ``../``, in ``anchored_local_path``,
``declaring_parent: local:`` and the ledger owners (source: #5418 scope
record). A package outside the repository keeps its absolute identity.
CELL: OP=install SRC=local PRIM=skill SCOPE=project LOCK=absent|legacy-absolute
GRAPH=transitive-sibling REPO=git|none FROZEN=on|off

A lock written with the absolute form is accepted read-only by ``install
--frozen`` and ``audit --ci``; one plain install re-keys it in place (same
owners, no file deleted, the apm_modules slot moved) and the next install
is a byte no-op.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.integration._install_audit_replay_kit import (
    ReplayWorld,
    lock_bytes,
    tree_digest,
    write,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-LAW-portability.byte_deterministic", "P-INV-I2"),
]

_APP = Path("apps") / "app"


def _skill_package(root: Path, name: str, deps: tuple[str, ...] = ()) -> None:
    manifest = f"name: {name}\nversion: 1.0.0\n"
    if deps:
        manifest += "dependencies:\n  apm:\n" + "".join(f"    - {dep}\n" for dep in deps)
    write(root / "apm.yml", manifest)
    write(
        root / f".apm/skills/{name}-skill/SKILL.md",
        f"---\nname: {name}-skill\ndescription: Skill of {name}\n---\n# {name}\n",
    )


def _monorepo(world: ReplayWorld, root: Path, *, outside: Path | None = None) -> Path:
    """The issue's layout: app -> ../../packages/lib-a -> ../lib-b -> ../lib-c (+ outside)."""
    _skill_package(root / "packages" / "lib-c", "lib-c")
    _skill_package(root / "packages" / "lib-b", "lib-b", ("../lib-c",))
    lib_a_deps = ("../lib-b",) if outside is None else ("../lib-b", outside.as_posix())
    _skill_package(root / "packages" / "lib-a", "lib-a", lib_a_deps)
    app = root / _APP
    write(
        app / "apm.yml",
        "name: app\nversion: 1.0.0\ntargets: [claude]\n"
        "dependencies:\n  apm:\n    - ../../packages/lib-a\n",
    )
    (app / ".claude").mkdir(parents=True)
    return app


def _transitive_rows(app: Path) -> dict[str, dict]:
    lock = load_yaml(app / "apm.lock.yaml")
    return {row["name"]: row for row in lock["dependencies"] if row.get("anchored_local_path")}


def _owners(app: Path) -> list[tuple[str, tuple[str, ...], str]]:
    lock = load_yaml(app / "apm.lock.yaml")
    return [
        (row["value"], tuple(row["owners"]), row["active_owner"]) for row in lock["deployments"]
    ]


def _deployed(app: Path) -> dict[str, bytes]:
    """Every file outside apm_modules, .git and the lockfile, with its bytes."""
    files = tree_digest(app)
    files.pop("apm.lock.yaml")
    return files


def _slots(app: Path) -> set[str]:
    return {path.name for path in (app / "apm_modules" / "_local").iterdir() if path.is_dir()}


def test_two_clones_write_byte_identical_lockfiles(tmp_path: Path, apm_binary_path: Path) -> None:
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    repo_a = world.sources / "A"
    app_a = _monorepo(world, repo_a)
    world.git(repo_a, "init", "-q", "--initial-branch=main")
    repo_b = world.sources / "elsewhere" / "deeper" / "B"
    shutil.copytree(repo_a, repo_b, symlinks=True)
    app_b = repo_b / _APP

    world.apm("install", cwd=app_a)
    world.apm("install", cwd=app_b)

    text = (app_a / "apm.lock.yaml").read_text(encoding="utf-8")
    assert (app_b / "apm.lock.yaml").read_text(encoding="utf-8") == text
    assert str(world.sources) not in text
    assert str(world.sources.resolve()) not in text
    rows = _transitive_rows(app_a)
    assert rows["lib-b"]["anchored_local_path"] == "../../packages/lib-b"
    assert rows["lib-b"]["declaring_parent"] == "../../packages/lib-a"
    assert rows["lib-c"]["anchored_local_path"] == "../../packages/lib-c"
    assert rows["lib-c"]["declaring_parent"] == "local:../../packages/lib-b"

    before = lock_bytes(app_a)
    world.apm("install", cwd=app_a)
    assert lock_bytes(app_a) == before, "I2: a converged install rewrote the lockfile"
    world.apm("install", "--frozen", cwd=app_a)
    world.audit_clean(cwd=app_a)


def test_outside_repository_and_no_repository_keep_absolute(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    outside = world.sources / "outside" / "lib-x"
    _skill_package(outside, "lib-x")
    repo = world.sources / "R"
    app = _monorepo(world, repo, outside=outside)
    world.git(repo, "init", "-q", "--initial-branch=main")
    world.apm("install", cwd=app)
    text = (app / "apm.lock.yaml").read_text(encoding="utf-8")
    assert f"anchored_local_path: {outside.resolve().as_posix()}" in text
    assert "anchored_local_path: ../../packages/lib-c" in text
    anchors = {row["anchored_local_path"] for row in _transitive_rows(app).values()}
    assert anchors == {
        "../../packages/lib-b",
        "../../packages/lib-c",
        outside.resolve().as_posix(),
    }

    plain = world.sources / "plain"
    plain_app = _monorepo(world, plain)
    world.apm("install", cwd=plain_app)
    plain_text = (plain_app / "apm.lock.yaml").read_text(encoding="utf-8")
    assert "anchored_local_path: ../" not in plain_text
    rows = _transitive_rows(plain_app)
    assert rows["lib-b"]["anchored_local_path"] == (
        (plain / "packages" / "lib-b").resolve().as_posix()
    )


def test_absolute_lock_is_rekeyed_in_place_once(tmp_path: Path, apm_binary_path: Path) -> None:
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    repo = world.sources / "R"
    app = _monorepo(world, repo)
    # Written before the repository exists: the pre-#5418 absolute form.
    world.apm("install", cwd=app)
    legacy, legacy_owners, legacy_slots = lock_bytes(app), _owners(app), _slots(app)
    absolute = {name: row["anchored_local_path"] for name, row in _transitive_rows(app).items()}
    assert all(Path(anchor).is_absolute() for anchor in absolute.values()), absolute
    deployed = _deployed(app)
    world.git(repo, "init", "-q", "--initial-branch=main")

    # Read path, on a second app in the same repository holding the same lock:
    # accepted as locked, nothing written.
    reader = repo / "apps" / "reader"
    shutil.copytree(app, reader, symlinks=True)
    world.apm("install", "--frozen", cwd=reader)
    world.audit_clean(cwd=reader)
    assert lock_bytes(reader) == legacy
    assert _deployed(reader) == deployed
    assert _slots(app) == legacy_slots

    # Write path: one install re-keys the rows in place.
    world.apm("install", cwd=app)
    rows = _transitive_rows(app)
    assert rows["lib-b"]["anchored_local_path"] == "../../packages/lib-b"
    assert rows["lib-c"]["anchored_local_path"] == "../../packages/lib-c"
    assert rows["lib-c"]["declaring_parent"] == "local:../../packages/lib-b"
    renames = {
        f"local:{absolute[name]}": f"local:{rows[name]['anchored_local_path']}" for name in rows
    }
    assert _owners(app) == [
        (value, tuple(renames.get(owner, owner) for owner in owners), renames.get(active, active))
        for value, owners, active in legacy_owners
    ]
    assert _deployed(app) == deployed, "a re-key must not redeploy or delete a file"
    slots = _slots(app)
    assert len(slots) == len(legacy_slots)
    assert not (legacy_slots - {"lib-a"}) & slots, "the legacy slot must move, not stay"

    rekeyed = (app / "apm.lock.yaml").read_bytes()
    world.apm("install", cwd=app)
    assert (app / "apm.lock.yaml").read_bytes() == rekeyed, "I2: the re-key must happen once"
    assert _slots(app) == slots
    world.apm("install", "--frozen", cwd=app)
    world.audit_clean(cwd=app)
