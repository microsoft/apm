"""Install resolves a survivor equally near parents declare after prune (#5238 item 1).

PROMISE: P-OWN-orphan-package-selection -- after ``apm prune`` with a dropped
parent, a local survivor that several parents in different directories
declare at one depth still installs: ``apm install --frozen`` and
``apm install --no-frozen`` both succeed, no surviving package or lock row is
lost, the lock records what a fresh resolve of the same manifest records, and
a second ``apm prune`` plus ``apm install --frozen`` change no lock bytes
(source: #5238 scope record, item 1; fixed by #5390).
CELL: OP=prune,install SRC=local PRIM=skill SCOPE=project LOCK=consistent
FROZEN=on,off GRAPH=multi-parent-survivor | variants: manifest order swapped,
a third equally near parent, a non-ASCII directory, Windows separators.

Graph: dev ``d -> ../e -> ../f``, ``a -> ../sub/z -> ../../f`` and
``b -> ../m -> ../f``. Dropping ``./vendor/d`` leaves ``f`` reachable only
through ``z`` and ``m``, both at depth 2, each spelling ``f`` differently.
"""

from __future__ import annotations

import shutil
import unicodedata
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._install_audit_replay_kit import ReplayWorld, lock_bytes

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-OWN-orphan-package-selection", "P-INV-I2", "P-INV-I3"),
]

_E_ACUTE = unicodedata.normalize("NFC", "\u00e9")
_BASE = (
    ("f", "f", ()),
    ("e", "e", ("../f",)),
    ("d", "d", ("../e",)),
    ("sub/z", "z", ("../../f",)),
    ("a", "a", ("../sub/z",)),
    ("m", "m", ("../f",)),
    ("b", "b", ("../m",)),
)
_SURVIVORS = {"a", "b", "f", "m", "z"}
_VARIANTS = {
    "base": (_BASE, ("d", "a", "b"), _SURVIVORS),
    "manifest-order-swapped": (_BASE, ("b", "a", "d"), _SURVIVORS),
    "third-equally-near-parent": (
        (*_BASE, ("t/u/w", "w", ("../../../f",)), ("c", "c", ("../t/u/w",))),
        ("d", "c", "a", "b"),
        _SURVIVORS | {"c", "w"},
    ),
    "non-ascii-directory": (
        tuple(
            (
                f"f{_E_ACUTE}" if path == "f" else path,
                name,
                tuple(dep.replace("/f", f"/f{_E_ACUTE}") for dep in deps),
            )
            for path, name, deps in _BASE
        ),
        ("d", "a", "b"),
        _SURVIVORS,
    ),
    "windows-separators": (
        tuple(
            (
                path,
                name,
                tuple(dep.replace("/", "\\") if name in ("z", "m") else dep for dep in deps),
            )
            for path, name, deps in _BASE
        ),
        ("d", "a", "b"),
        _SURVIVORS,
    ),
}
_F_SKILL = ".claude/skills/f-skill/SKILL.md"


def _skill_package(root: Path, name: str, deps: tuple[str, ...]) -> None:
    manifest: dict = {"name": name, "version": "1.0.0"}
    if deps:
        manifest["dependencies"] = {"apm": list(deps)}
    root.mkdir(parents=True, exist_ok=True)
    dump_yaml(manifest, root / "apm.yml")
    skill = root / ".apm" / "skills" / f"{name}-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(
        f"---\nname: {name}-skill\ndescription: Skill of {name}\n---\n# {name}\n",
        encoding="utf-8",
    )


def _manifest(project: Path, tops: tuple[str, ...]) -> None:
    dump_yaml(
        {
            "name": "proj",
            "version": "1.0.0",
            "targets": ["claude"],
            "dependencies": {"apm": []},
            "devDependencies": {"apm": [f"./vendor/{top}" for top in tops]},
        },
        project / "apm.yml",
    )


def _rows(project: Path) -> dict[str, dict]:
    """Lock rows by package name; every package in these graphs has a distinct name."""
    lock = load_yaml(project / "apm.lock.yaml")
    return {row["name"]: row for row in lock["dependencies"]}


def _fresh_resolve(world: ReplayWorld) -> bytes:
    """The lock a first install of the same manifest and sources records."""
    fresh = world.sources / "fresh"
    shutil.copytree(
        world.project,
        fresh,
        ignore=shutil.ignore_patterns("apm.lock.yaml", "apm_modules", ".claude"),
    )
    (fresh / ".claude").mkdir()
    world.apm("install", cwd=fresh)
    return lock_bytes(fresh)


@pytest.mark.parametrize(
    ("variant", "hash_seed"),
    [(name, str(seed)) for seed, name in enumerate(_VARIANTS)],
    ids=list(_VARIANTS),
)
def test_install_resolves_a_survivor_of_equally_near_parents_after_prune(
    tmp_path: Path, apm_binary_path: Path, variant: str, hash_seed: str
) -> None:
    """Prune d, then --frozen and --no-frozen pass, nothing is lost and the lock converges."""
    graph, tops, survivors = _VARIANTS[variant]
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    world.env["PYTHONHASHSEED"] = hash_seed
    for path, name, deps in graph:
        _skill_package(world.project / "vendor" / path, name, deps)
    _manifest(world.project, tops)
    (world.project / ".claude").mkdir()
    world.apm("install")
    assert survivors | {"d", "e"} == set(_rows(world.project))
    _manifest(world.project, tuple(top for top in tops if top != "d"))

    world.apm("prune")
    assert survivors <= set(_rows(world.project)), sorted(_rows(world.project))
    pruned = lock_bytes(world.project)
    world.apm("install", "--frozen")
    assert lock_bytes(world.project) == pruned
    assert (world.project / _F_SKILL).is_file()

    world.apm("install", "--no-frozen")
    rows = _rows(world.project)
    assert set(rows) == survivors, sorted(rows)
    assert rows["f"]["resolved_by"] in {row["repo_url"] for row in rows.values()}, rows["f"]
    assert (world.project / _F_SKILL).is_file()
    assert not (world.project / ".claude/skills/e-skill").exists()
    settled = lock_bytes(world.project)
    assert settled == _fresh_resolve(world)

    world.apm("prune")
    world.apm("install", "--frozen")
    assert lock_bytes(world.project) == settled
    world.apm("install", "--no-frozen")
    assert lock_bytes(world.project) == settled
    world.audit_clean()
