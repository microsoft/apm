"""A monorepo lockfile does not depend on the checkout location (#5418).

PROMISE: P-LAW-portability.byte_deterministic -- two clones of one repository
at different paths write a byte-identical apm.lock.yaml: a local package
inside the repository but outside the project root (a monorepo sibling) is
recorded relative to the project root with ``../``, in ``anchored_local_path``,
``declaring_parent: local:`` and the ledger owners (source: #5418 scope
record). A package outside the repository keeps its absolute identity.
CELL: OP=install SRC=local PRIM=skill SCOPE=project LOCK=absent
GRAPH=transitive-sibling REPO=git|none
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.integration._install_audit_replay_kit import ReplayWorld, lock_bytes, write

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
    """The issue's layout: app -> ../../packages/lib-a -> ../lib-b (and an outside dep)."""
    _skill_package(root / "packages" / "lib-b", "lib-b")
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


def _transitive_rows(app: Path) -> list[dict]:
    lock = load_yaml(app / "apm.lock.yaml")
    return [row for row in lock["dependencies"] if row.get("anchored_local_path")]


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
    (row,) = _transitive_rows(app_a)
    assert row["anchored_local_path"] == "../../packages/lib-b"
    assert row["declaring_parent"] == "../../packages/lib-a"

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
    assert "anchored_local_path: ../../packages/lib-b" in text
    anchors = {row["anchored_local_path"] for row in _transitive_rows(app)}
    assert anchors == {"../../packages/lib-b", outside.resolve().as_posix()}

    plain = world.sources / "plain"
    plain_app = _monorepo(world, plain)
    world.apm("install", cwd=plain_app)
    plain_text = (plain_app / "apm.lock.yaml").read_text(encoding="utf-8")
    assert "anchored_local_path: ../" not in plain_text
    (row,) = _transitive_rows(plain_app)
    assert row["anchored_local_path"] == (plain / "packages" / "lib-b").resolve().as_posix()
