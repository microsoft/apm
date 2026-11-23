"""Prune refuses to remove a replaced package's only parent, changing nothing (#5140).

PROMISE: P-INV-I7 -- a failed command preserves state. ``apm prune`` that
removes the only parent of a package a root ``replace`` entry redirects must
refuse, exit 1 and name the cause, with ``apm_modules``, ``apm.lock.yaml`` and the
deployed files byte-identical; it never deletes package directories and then
fails the lockfile write (source: #5140 scope record).
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
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
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


def _manifest(world: ReplayWorld, deps: list[str], **extra: object) -> None:
    dump_yaml(
        {
            "$schema": _SCHEMA,
            "name": "proj",
            "version": "1.0.0",
            "targets": ["claude", "copilot"],
            "dependencies": {"apm": deps},
            "replace": [
                {"for": {"git": "https://github.com/test/r"}, "with": {"path": "./vendor/r"}}
            ],
            **extra,
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


def _world(tmp_path: Path, apm_binary_path: Path) -> ReplayWorld:
    """Install ``./vendor/d -> test/r`` with ``test/r`` replaced by ``./vendor/r``."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    world.publish("r", _skill_package)
    vendor = world.project / "vendor"
    _skill_package(vendor / "r", "r")
    _skill_package(vendor / "d", "d", ("test/r",))
    (world.project / ".claude").mkdir()
    (world.project / ".github").mkdir()
    _manifest(world, ["./vendor/d"])
    world.apm("install")
    rows = LockFile.read(world.project / "apm.lock.yaml").dependencies
    assert any(key.endswith("test/r") or key == "test/r" for key in rows), sorted(rows)
    return world


def _assert_refused_with_nothing_changed(world: ReplayWorld) -> None:
    modules, lock = _modules(world.project), lock_bytes(world.project)
    tree = tree_digest(world.project)

    result = world.apm("prune", expect=None)

    evidence = f"exit={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    # The lockfile owner rejects the graph, so prune refuses before it changes anything.
    assert result.returncode == 1, evidence
    assert "Nothing was changed" in result.stdout + result.stderr, evidence
    assert "Replacement graph parent for test/r is absent" in result.stderr, evidence
    assert _modules(world.project) == modules, evidence
    assert lock_bytes(world.project) == lock, evidence
    assert tree_digest(world.project) == tree, evidence


def test_pruning_a_replaced_packages_parent_refuses_with_nothing_changed(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _world(tmp_path, apm_binary_path)
    _manifest(world, [])

    _assert_refused_with_nothing_changed(world)

    # The printed advice works: drop the replace entry, rebuild the lockfile, prune.
    manifest = load_yaml(world.project / "apm.yml")
    manifest.pop("replace")
    dump_yaml(manifest, world.project / "apm.yml")
    world.apm("install")
    world.apm("prune")
    assert not (world.project / "apm_modules/_local/d").exists()
    pruned = LockFile.read(world.project / "apm.lock.yaml")
    rows = pruned.dependencies if pruned is not None else {}
    assert not any(key.endswith("test/r") for key in rows), sorted(rows)


def _canvas_bundle(world: ReplayWorld) -> Path:
    """Pack a local bundle that ships a canvas extension for copilot."""
    producer = world.sources / "canvas-producer"
    _write(
        producer / "apm.yml",
        "name: canvasbundle\nversion: 1.0.0\ndescription: c\nlicense: MIT\n"
        "targets: [copilot]\ndependencies:\n  apm: []\n",
    )
    _write(
        producer / ".apm/skills/canvasbundle-s/SKILL.md",
        "---\nname: canvasbundle-s\ndescription: canvas fixture\n---\n# c\n",
    )
    _write(producer / ".apm/extensions/board/extension.mjs", "export default {};\n")
    (producer / ".github").mkdir()
    world.apm("install", "--no-policy", cwd=producer)
    world.apm("pack", "-o", str(world.sources / "canvas-out"), cwd=producer)
    return world.sources / "canvas-out" / "canvasbundle-1.0.0"


def test_the_refusal_comes_before_bundle_reconciliation_changes_anything(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A revoked bundle canvas prune would retire stays: the refusal runs first."""
    world = _world(tmp_path, apm_binary_path)
    world.apm("experimental", "enable", "canvas")
    world.apm("install", str(_canvas_bundle(world)))
    extension = world.project / ".github/extensions/board/extension.mjs"
    assert extension.is_file()
    # Revoke the bundle's canvas and drop d: prune would retire the extension.
    _manifest(world, [], executables={"allow": {}})

    _assert_refused_with_nothing_changed(world)
    assert extension.is_file()
