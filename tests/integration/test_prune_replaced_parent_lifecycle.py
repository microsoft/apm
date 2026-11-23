"""Prune of a replaced package's parent completes or changes nothing (#5140).

PROMISE: P-INV-I7 -- a failed command preserves state. ``apm prune`` that
removes the only parent of a package a root ``replace`` entry redirects must
either finish with a lockfile whose replacement graph is whole, or refuse with
``apm_modules``, ``apm.lock.yaml`` and the deployed files byte-identical; it never deletes package
directories and then fails the lockfile write (source: #5140 scope record).
CELL: OP=prune SRC=local+replace PRIM=skill SCOPE=project LOCK=consistent
GRAPH=replaced-transitive-of-pruned-parent

Graph: ``./vendor/d`` declares ``test/r``; ``apm.yml`` replaces ``test/r`` with
the local ``./vendor/r``. Dropping ``./vendor/d`` from apm.yml makes ``d`` an
orphan whose child ``r`` is the replaced package.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.yaml_io import dump_yaml
from tests.integration._install_audit_replay_kit import ReplayWorld, lock_bytes, tree_digest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I7", "P-INV-I1"),
]

_SCHEMA = (
    "https://microsoft.github.io/apm/specs/schemas/manifest-source-replacements-v1.schema.json"
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _skill_package(root: Path, name: str, deps: tuple[str, ...] = ()) -> None:
    manifest = f"name: {name}\nversion: 1.0.0\n"
    if deps:
        manifest += "dependencies:\n  apm:\n" + "".join(f"    - {dep}\n" for dep in deps)
    _write(root / "apm.yml", manifest)
    _write(
        root / f".apm/skills/{name}-skill/SKILL.md",
        f"---\nname: {name}-skill\ndescription: Skill of {name}\n---\n# {name}\n",
    )


def _manifest(world: ReplayWorld, deps: list[str]) -> None:
    dump_yaml(
        {
            "$schema": _SCHEMA,
            "name": "proj",
            "version": "1.0.0",
            "targets": ["claude"],
            "dependencies": {"apm": deps},
            "replace": [
                {"for": {"git": "https://github.com/test/r"}, "with": {"path": "./vendor/r"}}
            ],
        },
        world.project / "apm.yml",
    )


def _modules(project: Path) -> dict[str, bytes | None]:
    """Every entry under apm_modules: file bytes, or ``None`` for a directory."""
    root = project / "apm_modules"
    return {
        path.relative_to(root).as_posix(): (None if path.is_dir() else path.read_bytes())
        for path in sorted(root.rglob("*"))
    }


def test_pruning_a_replaced_packages_parent_completes_or_changes_nothing(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    world.publish("r", _skill_package)
    vendor = world.project / "vendor"
    _skill_package(vendor / "r", "r")
    _skill_package(vendor / "d", "d", ("test/r",))
    (world.project / ".claude").mkdir()
    _manifest(world, ["./vendor/d"])
    world.apm("install")
    rows = LockFile.read(world.project / "apm.lock.yaml").dependencies
    assert any(key.endswith("test/r") or key == "test/r" for key in rows), sorted(rows)
    _manifest(world, [])
    modules, lock = _modules(world.project), lock_bytes(world.project)
    tree = tree_digest(world.project)

    result = world.apm("prune", expect=None)

    evidence = f"exit={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    if result.returncode != 0:
        # A refusal changes nothing.
        assert _modules(world.project) == modules, evidence
        assert lock_bytes(world.project) == lock, evidence
        assert tree_digest(world.project) == tree, evidence
        return
    # A completed prune leaves a lockfile whose replacement graph is whole.
    pruned = LockFile.read(world.project / "apm.lock.yaml")
    assert pruned is not None, evidence
    keys = set(pruned.dependencies)
    for key, dep in pruned.dependencies.items():
        missing = [parent for parent in dep.parent_keys if parent and parent not in keys]
        assert missing == [], f"{key} keeps absent parents {missing}\n{evidence}"
    world.audit_clean()
