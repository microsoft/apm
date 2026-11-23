"""Prune removes a transitive local dependency no survivor declares (#4989).

PROMISE: P-OWN-orphan-package-selection -- ``apm prune`` treats a transitive
local dependency that no surviving package declares any more as an orphan:
its lock row goes, and its deployed files go only where deployment evidence
(receipts or the locked render) proves APM wrote them. An edited or unproven
file is kept and named; a partial graph keeps everything and reports it
(source: #4989 scope record).
CELL: OP=prune SRC=local PRIM=skill SCOPE=project LOCK=consistent
GRAPH=transitive-local-orphan | path variants: symlinked or junctioned dir,
mixed case, NFC and NFD names, and the same directory spelled two ways.

Graph: dev ``d -> ../e``, ``e -> ../f`` and dev ``a -> ../f``. Removing
``./vendor/d`` from apm.yml leaves ``e`` unreachable while ``f`` is still
needed by ``a``.
"""

from __future__ import annotations

import os
import shutil
import sys
import unicodedata
from pathlib import Path

import pytest

from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.content_hash import compute_file_hash
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._install_audit_replay_kit import ReplayWorld, lock_bytes, tree_digest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-OWN-orphan-package-selection", "P-INV-I1", "P-INV-I3"),
]

_E_SKILL = ".claude/skills/e-skill"
_F_SKILL = ".claude/skills/f-skill"
_NO_EVIDENCE = "no local deployment receipt or render of the locked package version"
_NFC = unicodedata.normalize("NFC", "\u00e9")
_NFD = unicodedata.normalize("NFD", "\u00e9")


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


def _manifest(world: ReplayWorld, dev: list[str]) -> None:
    dump_yaml(
        {
            "name": "proj",
            "version": "1.0.0",
            "targets": ["claude"],
            "dependencies": {"apm": []},
            "devDependencies": {"apm": dev},
        },
        world.project / "apm.yml",
    )


def _link_dir(link: Path, target: Path) -> None:
    """A directory symlink, or an NTFS junction where symlinks need privileges."""
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise
        import _winapi

        _winapi.CreateJunction(str(target), str(link))


def _fs_folds(spelling: str, other: str, tmp_path: Path) -> bool:
    """True when this filesystem opens *other* as the directory *spelling*."""
    probe = tmp_path / "fold-probe"
    (probe / spelling).mkdir(parents=True)
    return (probe / other).is_dir()


def _world(
    tmp_path: Path,
    apm_binary_path: Path,
    *,
    e_dir: str = "e",
    f_dir: str = "f",
    a_spells_f: str | None = None,
) -> ReplayWorld:
    """Install dev d -> e -> f and dev a -> f, then drop ./vendor/d from apm.yml."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    vendor = world.project / "vendor"
    _skill_package(vendor / f_dir, "f")
    _skill_package(vendor / e_dir, "e", (f"../{f_dir}",))
    _skill_package(vendor / "d", "d", (f"../{e_dir}",))
    _skill_package(vendor / "a", "a", (f"../{a_spells_f or f_dir}",))
    _manifest(world, ["./vendor/d", "./vendor/a"])
    (world.project / ".claude").mkdir()
    world.apm("install")
    assert _row_named(world, "e")[1]["declaring_parent"] == "./vendor/d"
    _manifest(world, ["./vendor/a"])
    return world


def _rows(world: ReplayWorld) -> dict[str, dict]:
    lock = load_yaml(world.project / "apm.lock.yaml")
    return {row["repo_url"]: row for row in lock["dependencies"]}


def _row_named(world: ReplayWorld, name: str) -> tuple[str, dict]:
    return next((key, row) for key, row in _rows(world).items() if row.get("name") == name)


def _installed(world: ReplayWorld) -> set[str]:
    return {path.name for path in (world.project / "apm_modules" / "_local").iterdir()}


def _output(result) -> str:
    return " ".join((result.stdout + result.stderr).split())


def _assert_frozen_then_no_frozen_converge(world: ReplayWorld) -> None:
    """Cell 4: frozen passes after prune and a following --no-frozen keeps the lock bytes."""
    pruned = lock_bytes(world.project)
    world.apm("install", "--frozen")
    assert lock_bytes(world.project) == pruned
    world.apm("install", "--no-frozen")
    assert lock_bytes(world.project) == pruned


def _assert_orphan_pruned(world: ReplayWorld) -> None:
    rows = _rows(world)
    assert "_local/e" not in rows, sorted(rows)
    assert {"_local/a", "_local/f"} <= set(rows), sorted(rows)
    assert not (world.project / _E_SKILL).exists()
    assert (world.project / _F_SKILL / "SKILL.md").is_file()
    deployed = {path for row in rows.values() for path in row.get("deployed_files") or []}
    assert not any(path.startswith(_E_SKILL) for path in deployed), deployed


def test_prune_removes_transitive_local_orphan_and_keeps_shared_dependency(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Cells 1 and 4: e's row and proven files go; f stays; frozen and no-frozen agree."""
    world = _world(tmp_path, apm_binary_path)

    preview = _output(world.apm("prune", "--dry-run"))
    assert "Found 2 orphaned package(s)" in preview, preview
    installed = _installed(world)

    before = tree_digest(world.project)
    pruned = _output(world.apm("prune"))

    assert "Removed _local/d" in pruned, pruned
    assert pruned.count("Removed _local/") == 2, pruned
    assert len(_installed(world)) == len(installed) - 2
    _assert_orphan_pruned(world)
    after = tree_digest(world.project)
    changed = {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}
    assert changed == {
        "apm.lock.yaml",
        f"{_E_SKILL}/SKILL.md",
        ".claude/skills/d-skill/SKILL.md",
    }, sorted(changed)
    assert "nothing to prune" in _output(world.apm("prune", "--dry-run")).lower()
    _assert_frozen_then_no_frozen_converge(world)
    world.audit_clean()


def test_prune_keeps_an_edited_file_of_the_orphan_and_names_it(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Cell 2: a user-edited file of e is kept with a diagnostic; e's row still goes."""
    world = _world(tmp_path, apm_binary_path)
    edited = world.project / _E_SKILL / "SKILL.md"
    edited.write_text(edited.read_text(encoding="utf-8") + "\nmine\n", encoding="utf-8")
    edited_bytes = edited.read_bytes()

    pruned = _output(world.apm("prune"))

    assert (
        f"Skipped removing {_E_SKILL}/SKILL.md: file has been edited since APM deployed it"
    ) in pruned, pruned
    assert edited.read_bytes() == edited_bytes
    assert "_local/e" not in _rows(world)
    assert (world.project / _F_SKILL / "SKILL.md").is_file()


def test_prune_keeps_an_orphan_file_without_deployment_evidence(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Cell 3: a file e's row claims but APM cannot prove it wrote is kept and named."""
    world = _world(tmp_path, apm_binary_path)
    forged = ".claude/skills/e-skill/notes.md"
    user = world.project / forged
    _write(user, "# My own notes\n")
    lock_path = world.project / "apm.lock.yaml"
    lock = LockFile.read(lock_path)
    assert lock is not None
    (row,) = [dep for dep in lock.dependencies.values() if dep.repo_url == "_local/e"]
    row.deployed_files.append(forged)
    row.deployed_file_hashes[forged] = compute_file_hash(user)
    DeploymentLedgerCodec.refresh_from_legacy(lock)
    lock.write(lock_path)

    pruned = _output(world.apm("prune"))

    assert f"Kept {forged}: {_NO_EVIDENCE}" in pruned, pruned
    assert user.read_text(encoding="utf-8") == "# My own notes\n"
    assert not (world.project / _E_SKILL / "SKILL.md").exists()
    assert "_local/e" not in _rows(world)


def test_prune_with_a_partial_graph_keeps_the_orphan_and_reports(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A survivor whose manifest cannot be read keeps every local transitive and says so."""
    world = _world(tmp_path, apm_binary_path)
    (world.project / "vendor" / "a" / "apm.yml").write_text("name: [\n", encoding="utf-8")

    pruned = _output(world.apm("prune"))

    assert "_local/e" in _rows(world)
    assert (world.project / _E_SKILL / "SKILL.md").is_file()
    assert "Retained _local/e: the dependency graph could not be read completely" in pruned, pruned
    assert pruned.count("Retained _local/e:") == 1, pruned


def test_prune_keeps_only_the_local_row_whose_anchor_cannot_resolve(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A lock row whose declaring parent is already gone is kept and named; the rest is pruned."""
    world = _world(tmp_path, apm_binary_path)
    lock_path = world.project / "apm.lock.yaml"
    lock = LockFile.read(lock_path)
    assert lock is not None
    (d_key,) = [key for key, dep in lock.dependencies.items() if dep.repo_url == "_local/d"]
    lock.dependencies.pop(d_key)
    lock.write(lock_path)

    pruned = _output(world.apm("prune"))

    assert "Retained _local/e: the dependency graph could not be read completely" in pruned, pruned
    assert "Retained _local/f:" not in pruned, pruned
    rows = _rows(world)
    assert {"_local/a", "_local/e", "_local/f"} == set(rows), sorted(rows)
    assert (world.project / _E_SKILL / "SKILL.md").is_file()
    assert (world.project / _F_SKILL / "SKILL.md").is_file()


@pytest.mark.windows_compat
@pytest.mark.parametrize(
    ("e_dir", "f_dir"),
    [
        pytest.param("E-Pkg", "F-Pkg", id="mixed-case"),
        pytest.param(f"e{_NFC}", f"f{_NFC}", id="nfc"),
        pytest.param(f"e{_NFD}", f"f{_NFD}", id="nfd"),
    ],
)
def test_prune_matches_exact_lock_keys_for_name_variants(
    tmp_path: Path, apm_binary_path: Path, e_dir: str, f_dir: str
) -> None:
    """Path cells: case and Unicode-normalization spellings keep their exact lock keys."""
    world = _world(tmp_path, apm_binary_path, e_dir=e_dir, f_dir=f_dir)
    e_key, f_key = _row_named(world, "e")[0], _row_named(world, "f")[0]

    world.apm("prune")

    rows = _rows(world)
    assert e_key not in rows, sorted(rows)
    assert f_key in rows, sorted(rows)
    assert not (world.project / _E_SKILL).exists()
    assert (world.project / _F_SKILL / "SKILL.md").is_file()
    _assert_frozen_then_no_frozen_converge(world)


@pytest.mark.windows_compat
@pytest.mark.parametrize("linked", ["e", "f"])
def test_prune_through_a_symlinked_or_junctioned_local_dir(
    tmp_path: Path, apm_binary_path: Path, linked: str
) -> None:
    """Path cell: a linked package dir resolves to the same node as its target."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    vendor = world.project / "vendor"
    for name, deps in (("f", ()), ("e", ("../f",)), ("d", ("../e",)), ("a", ("../f",))):
        _skill_package(vendor / (f"{name}-real" if name == linked else name), name, deps)
    _link_dir(vendor / linked, vendor / f"{linked}-real")
    _manifest(world, ["./vendor/d", "./vendor/a"])
    (world.project / ".claude").mkdir()
    world.apm("install")
    _manifest(world, ["./vendor/a"])

    world.apm("prune")

    _assert_orphan_pruned(world)
    _assert_frozen_then_no_frozen_converge(world)


@pytest.mark.windows_compat
@pytest.mark.parametrize(
    ("e_spells_f", "a_spells_f"),
    [
        pytest.param("f", "f-link", id="symlink-alias"),
        pytest.param("F-Pkg", "f-pkg", id="case-alias"),
        pytest.param(f"f{_NFC}", f"f{_NFD}", id="nfc-alias"),
    ],
)
def test_prune_keeps_a_survivor_spelled_another_way(
    tmp_path: Path, apm_binary_path: Path, e_spells_f: str, a_spells_f: str
) -> None:
    """The shared dependency reached through another spelling of its dir is not an orphan."""
    if a_spells_f == "f-link":
        world = _world(tmp_path, apm_binary_path, f_dir=e_spells_f, a_spells_f=None)
        _link_dir(world.project / "vendor" / a_spells_f, world.project / "vendor" / e_spells_f)
        _skill_package(world.project / "vendor" / "a", "a", (f"../{a_spells_f}",))
    else:
        if not _fs_folds(e_spells_f, a_spells_f, tmp_path):
            pytest.skip("this filesystem keeps both spellings distinct")
        world = _world(tmp_path, apm_binary_path, f_dir=e_spells_f, a_spells_f=a_spells_f)

    world.apm("prune")

    rows = _rows(world)
    assert "_local/e" not in rows, sorted(rows)
    assert any(row.get("name") == "f" for row in rows.values()), sorted(rows)
    assert (world.project / _F_SKILL / "SKILL.md").is_file()


def test_prune_reparents_a_survivor_whose_recorded_parent_goes(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Equal-depth diamond: f was recorded under e; after prune it takes z, as install would."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    vendor = world.project / "vendor"
    for name, deps in (
        ("f", ()),
        ("e", ("../f",)),
        ("z", ("../f",)),
        ("d", ("../e",)),
        ("a", ("../z",)),
    ):
        _skill_package(vendor / name, name, deps)
    _manifest(world, ["./vendor/d", "./vendor/a"])
    (world.project / ".claude").mkdir()
    world.apm("install")
    assert _row_named(world, "f")[1]["resolved_by"] == "_local/e"
    _manifest(world, ["./vendor/a"])

    world.apm("prune")

    rows = _rows(world)
    assert "_local/e" not in rows, sorted(rows)
    assert _row_named(world, "f")[1]["resolved_by"] == "_local/z"
    assert (world.project / _F_SKILL / "SKILL.md").is_file()
    _assert_frozen_then_no_frozen_converge(world)


@pytest.mark.skipif(sys.platform == "win32", reason="needs an unprivileged symlink")
def test_prune_keeps_a_local_row_whose_source_is_a_symlink_loop(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A source directory that cannot resolve keeps its row and is named, not a crash."""
    world = _world(tmp_path, apm_binary_path)
    source = world.project / "vendor" / "e"
    shutil.rmtree(source)
    os.symlink(source, source, target_is_directory=True)

    pruned = _output(world.apm("prune"))

    assert "Retained _local/e:" in pruned, pruned
    assert "Traceback" not in pruned and "Failed to parse" not in pruned, pruned
    assert "_local/e" in _rows(world)
    assert "_local/d" not in _rows(world)
    assert (world.project / _E_SKILL / "SKILL.md").is_file()
    assert not (world.project / ".claude/skills/d-skill").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="needs an unprivileged symlink")
def test_prune_reparents_under_the_locked_row_of_an_aliased_parent(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """g's surviving parent f is reached through a symlink alias; it records f's own row."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    vendor = world.project / "vendor"
    for name, deps in (
        ("g", ()),
        ("f", ("../g",)),
        ("e", ("../f", "../g")),
        ("d", ("../e",)),
        ("z", ("../f-link",)),
        ("a", ("../z",)),
    ):
        _skill_package(vendor / name, name, deps)
    _link_dir(vendor / "f-link", vendor / "f")
    _manifest(world, ["./vendor/d", "./vendor/a"])
    (world.project / ".claude").mkdir()
    world.apm("install")
    _manifest(world, ["./vendor/a"])

    world.apm("prune")

    rows = _rows(world)
    # Diagnostic (temporary): the lock rows and on-disk hashes before the frozen replay.
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.deps.path_anchoring import resolve_local_dep_dirs
    from apm_cli.install.phases.local_content import compute_local_package_hash

    locked = LockFile.read(world.project / "apm.lock.yaml")
    for key, directory in sorted(resolve_local_dep_dirs(locked, world.project).items()):
        row = locked.dependencies[key]
        print(
            "DIAG",
            key,
            row.repo_url,
            row.local_path,
            row.declaring_parent,
            row.anchored_local_path,
            row.resolved_by,
            row.content_hash,
            directory,
            compute_local_package_hash(directory),
        )
    frozen = world.apm("install", "--frozen", expect=None)
    print("DIAG frozen", frozen.returncode, frozen.stdout, frozen.stderr)
    assert "_local/e" not in rows, sorted(rows)
    assert _row_named(world, "g")[1]["resolved_by"] in rows
    assert (world.project / ".claude/skills/g-skill/SKILL.md").is_file()
    _assert_frozen_then_no_frozen_converge(world)


@pytest.mark.xfail(
    strict=True,
    reason="a legacy row's new local_path changes its lock key; owner re-key tracked in #5126",
)
def test_prune_reparents_a_legacy_local_row_with_its_path(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A lock without declaring_parent: the survivor takes its new parent's path too."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    vendor = world.project / "vendor"
    for name, deps in (
        ("f", ()),
        ("e", ("../f",)),
        ("sub/z", ("../../f",)),
        ("d", ("../e",)),
        ("a", ("../sub/z",)),
    ):
        _skill_package(vendor / name, name.rsplit("/", 1)[-1], deps)
    _manifest(world, ["./vendor/d", "./vendor/a"])
    (world.project / ".claude").mkdir()
    world.apm("install")
    lock_path = world.project / "apm.lock.yaml"
    lock = load_yaml(lock_path)
    for row in lock["dependencies"]:
        row.pop("declaring_parent", None)
        row.pop("anchored_local_path", None)
    dump_yaml(lock, lock_path)
    _manifest(world, ["./vendor/a"])

    world.apm("prune")

    rows = _rows(world)
    assert "_local/e" not in rows, sorted(rows)
    assert _row_named(world, "f")[1]["local_path"] == "../../f"
    assert (world.project / _F_SKILL / "SKILL.md").is_file()
    world.apm("install", "--frozen")


_REPLACE_SCHEMA = (
    "https://microsoft.github.io/apm/specs/schemas/manifest-source-replacements-v1.schema.json"
)


def test_prune_keeps_a_local_child_of_a_retained_replaced_package(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """d -> test/r (replaced by local vendor/r) -> ../e: dropping d keeps r, so e stays too."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    vendor = world.project / "vendor"
    world.publish("r", lambda root, name: _skill_package(root, name))
    _skill_package(vendor / "e", "e")
    _skill_package(vendor / "r", "r", ("../e",))
    _skill_package(vendor / "d", "d", ("test/r",))
    manifest = {
        "$schema": _REPLACE_SCHEMA,
        "name": "proj",
        "version": "1.0.0",
        "targets": ["claude"],
        "dependencies": {"apm": []},
        "devDependencies": {"apm": ["./vendor/d"]},
        "replace": [{"for": {"git": "https://github.com/test/r"}, "with": {"path": "./vendor/r"}}],
    }
    dump_yaml(manifest, world.project / "apm.yml")
    (world.project / ".claude").mkdir()
    world.apm("install")
    assert "_local/e" in _rows(world)
    manifest["devDependencies"] = {"apm": []}
    dump_yaml(manifest, world.project / "apm.yml")
    lock = lock_bytes(world.project)

    # The real prune aborts writing the lock once d, the replacement's recorded
    # parent, is gone (#5140); the dry run shows the retention decision.
    preview = _output(world.apm("prune", "--dry-run"))

    assert "Found 1 orphaned package(s)" in preview, preview
    assert "_local/d (would be removed)" in preview, preview
    assert lock_bytes(world.project) == lock
    assert (world.project / _E_SKILL / "SKILL.md").is_file()
