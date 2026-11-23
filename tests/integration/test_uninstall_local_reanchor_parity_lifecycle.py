"""A re-anchoring uninstall leaves what a fresh install would write (#4999, #4889).

PROMISE: P-INV-I3 -- after ``apm uninstall ./vendor/e`` re-anchors a still-needed
local ``e``, a following ``apm install --no-frozen`` on the same inputs rewrites
neither ``apm.lock.yaml`` nor ``apm_modules/``: the materialization directory moved
to the re-anchored install path, declaring edges follow install's breadth-first
order across production and dev roots, and replacement requests follow the
post-uninstall resolver order (source: #4999 scope record).
CELL: OP=uninstall,install SRC=local PRIM=skill SCOPE=project LOCK=consistent
GRAPH=direct-also-transitive | cross-root diamond | replaced edge
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.integration._install_audit_replay_kit import ReplayWorld, lock_bytes

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I3", "P-INV-I1"),
]

_REPLACEMENTS_SCHEMA = (
    "https://microsoft.github.io/apm/specs/schemas/manifest-source-replacements-v1.schema.json"
)


def _skill_package(root: Path, name: str, deps: tuple[str, ...] = ()) -> None:
    manifest = f"name: {name}\nversion: 1.0.0\n"
    if deps:
        manifest += "dependencies:\n  apm:\n" + "".join(f"    - {dep}\n" for dep in deps)
    root.mkdir(parents=True, exist_ok=True)
    (root / "apm.yml").write_text(manifest, encoding="utf-8")
    skill = root / f".apm/skills/{name}-skill/SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(
        f"---\nname: {name}-skill\ndescription: Skill of {name}\n---\n# {name}\n",
        encoding="utf-8",
    )


def _modules(project: Path) -> dict[str, bytes]:
    """Every file under ``apm_modules/`` mapped to its bytes."""
    root = project / "apm_modules"
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _outside_modules(project: Path) -> dict[str, bytes]:
    """Every file outside ``apm_modules/`` but apm.yml and apm.lock.yaml, by its bytes."""
    return {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in sorted(project.rglob("*"))
        if path.is_file()
        and path.relative_to(project).parts[0] not in ("apm_modules", ".git")
        and path.relative_to(project).as_posix() not in ("apm.yml", "apm.lock.yaml")
    }


def _world(tmp_path: Path, apm_binary_path: Path, packages, manifest: dict) -> ReplayWorld:
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    for name, deps in packages:
        _skill_package(world.project / "vendor" / name, name, deps)
    dump_yaml(
        {"name": "proj", "version": "1.0.0", "targets": ["claude"], **manifest},
        world.project / "apm.yml",
    )
    (world.project / ".claude").mkdir()
    world.apm("install")
    return world


def _assert_no_frozen_rewrites_nothing(world: ReplayWorld) -> None:
    """A REAL ``apm install --no-frozen`` on the same inputs changes no lock or cache byte."""
    lock, modules = lock_bytes(world.project), _modules(world.project)
    world.apm("install", "--no-frozen")
    assert lock_bytes(world.project) == lock
    assert _modules(world.project) == modules
    world.apm("install", "--frozen")


def test_reanchor_moves_the_materialization_directory(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """e -> f direct, dev d -> e: e's apm_modules copy moves to its re-anchored path."""
    world = _world(
        tmp_path,
        apm_binary_path,
        (("f", ()), ("e", ("../f",)), ("d", ("../e",))),
        {"dependencies": {"apm": ["./vendor/e"]}, "devDependencies": {"apm": ["./vendor/d"]}},
    )
    outside = _outside_modules(world.project)

    world.apm("uninstall", "./vendor/e")

    assert not (world.project / "apm_modules/_local/e").exists()
    assert _outside_modules(world.project) == outside
    _assert_no_frozen_rewrites_nothing(world)


def test_reanchor_follows_breadth_first_declaring_edges_across_roots(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Production p -> q -> e -> f, dev b -> a -> f and d -> e: f's first edge is a's."""
    world = _world(
        tmp_path,
        apm_binary_path,
        (
            ("f", ()),
            ("e", ("../f",)),
            ("q", ("../e",)),
            ("p", ("../q",)),
            ("a", ("../f",)),
            ("b", ("../a",)),
            ("d", ("../e",)),
        ),
        {
            "dependencies": {"apm": ["./vendor/p", "./vendor/e"]},
            "devDependencies": {"apm": ["./vendor/b", "./vendor/d"]},
        },
    )

    world.apm("uninstall", "./vendor/e")

    _assert_no_frozen_rewrites_nothing(world)


def test_reanchor_rebuilds_replacement_requests_in_resolver_order(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Direct e -> replaced r, dev d -> e and a -> r: r's requests follow install's order."""
    world = _world(
        tmp_path,
        apm_binary_path,
        (("r", ()), ("e", ("test/r",)), ("d", ("../e",)), ("a", ("test/r",))),
        {
            "$schema": _REPLACEMENTS_SCHEMA,
            "dependencies": {"apm": ["./vendor/e"]},
            "devDependencies": {"apm": ["./vendor/d", "./vendor/a"]},
            "replace": [
                {"for": {"git": "https://github.com/test/r"}, "with": {"path": "./vendor/r"}}
            ],
        },
    )

    world.apm("uninstall", "./vendor/e")

    _assert_no_frozen_rewrites_nothing(world)


def _aliased_package(root: Path, name: str, child: str, alias: str) -> None:
    _skill_package(root, name)
    (root / "apm.yml").write_text(
        f"name: {name}\nversion: 1.0.0\ndependencies:\n  apm:\n"
        f"    - path: {child}\n      alias: {alias}\n",
        encoding="utf-8",
    )


def test_a_reparented_descendant_under_another_alias_refuses(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """e declares f as old-f, dev a declares f as new-f: f's winning edge installs elsewhere."""
    from tests.integration._install_audit_replay_kit import tree_digest

    world = ReplayWorld.create(tmp_path, apm_binary_path)
    vendor = world.project / "vendor"
    _skill_package(vendor / "f", "f")
    _aliased_package(vendor / "e", "e", "../f", "old-f")
    _aliased_package(vendor / "a", "a", "../f", "new-f")
    _skill_package(vendor / "d", "d", ("../e",))
    dump_yaml(
        {
            "name": "proj",
            "version": "1.0.0",
            "targets": ["claude"],
            "dependencies": {"apm": ["./vendor/e"]},
            "devDependencies": {"apm": ["./vendor/d", "./vendor/a"]},
        },
        world.project / "apm.yml",
    )
    (world.project / ".claude").mkdir()
    world.apm("install")
    before = (tree_digest(world.project), _modules(world.project))

    refused = world.apm("uninstall", "./vendor/e", expect=1)

    output = " ".join((refused.stdout + refused.stderr).split())
    assert "under another name (new-f)" in output, output
    assert (tree_digest(world.project), _modules(world.project)) == before


def test_a_retained_descendant_without_its_apm_modules_copy_refuses(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """e -> f direct, dev d -> e; f's copy is gone: install would recreate it, so refuse."""
    import shutil

    from tests.integration._install_audit_replay_kit import tree_digest

    world = _world(
        tmp_path,
        apm_binary_path,
        (("f", ()), ("e", ("../f",)), ("d", ("../e",))),
        {"dependencies": {"apm": ["./vendor/e"]}, "devDependencies": {"apm": ["./vendor/d"]}},
    )
    (copy,) = (
        path.parent
        for path in (world.project / "apm_modules").rglob("apm.yml")
        if path.read_text(encoding="utf-8").startswith("name: f\n")
    )
    shutil.rmtree(copy)
    before = (tree_digest(world.project), _modules(world.project))

    refused = world.apm("uninstall", "./vendor/e", expect=1)

    output = " ".join((refused.stdout + refused.stderr).split())
    assert "is missing" in output, output
    assert (tree_digest(world.project), _modules(world.project)) == before


def test_a_retained_descendant_with_a_stale_copy_refuses(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """e -> f direct, dev d -> e; f's source changed after install: its copy is stale."""
    from tests.integration._install_audit_replay_kit import tree_digest

    world = _world(
        tmp_path,
        apm_binary_path,
        (("f", ()), ("e", ("../f",)), ("d", ("../e",))),
        {"dependencies": {"apm": ["./vendor/e"]}, "devDependencies": {"apm": ["./vendor/d"]}},
    )
    skill = world.project / "vendor/f/.apm/skills/f-skill/SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8") + "\nEdited.\n", encoding="utf-8")
    before = (tree_digest(world.project), _modules(world.project))

    refused = world.apm("uninstall", "./vendor/e", expect=1)

    output = " ".join((refused.stdout + refused.stderr).split())
    assert "differs from its apm_modules copy" in output, output
    assert (tree_digest(world.project), _modules(world.project)) == before


def test_a_retained_descendant_whose_excluded_files_changed_refuses(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """f's .git/HEAD changes after install: the hash ignores it, the copier does not."""
    from tests.integration._install_audit_replay_kit import tree_digest

    world = ReplayWorld.create(tmp_path, apm_binary_path)
    for name, deps in (("f", ()), ("e", ("../f",)), ("d", ("../e",))):
        _skill_package(world.project / "vendor" / name, name, deps)
    head = world.project / "vendor/f/.git/HEAD"
    head.parent.mkdir(parents=True)
    head.write_text("ref: refs/heads/main\n", encoding="utf-8")
    dump_yaml(
        {
            "name": "proj",
            "version": "1.0.0",
            "targets": ["claude"],
            "dependencies": {"apm": ["./vendor/e"]},
            "devDependencies": {"apm": ["./vendor/d"]},
        },
        world.project / "apm.yml",
    )
    (world.project / ".claude").mkdir()
    world.apm("install")
    head.write_text("ref: refs/heads/other\n", encoding="utf-8")
    before = (tree_digest(world.project), _modules(world.project))

    refused = world.apm("uninstall", "./vendor/e", expect=1)

    output = " ".join((refused.stdout + refused.stderr).split())
    assert "differs from its apm_modules copy" in output, output
    assert (tree_digest(world.project), _modules(world.project)) == before


def test_a_reanchor_whose_deployed_links_point_into_the_moving_copy_refuses(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """e's skill links docs/ref.md, rendered into apm_modules/_local/e: moving it dangles."""
    from tests.integration._install_audit_replay_kit import tree_digest

    world = ReplayWorld.create(tmp_path, apm_binary_path)
    vendor = world.project / "vendor"
    _skill_package(vendor / "f", "f")
    _skill_package(vendor / "e", "e", ("../f",))
    _skill_package(vendor / "d", "d", ("../e",))
    (vendor / "e/docs").mkdir()
    (vendor / "e/docs/ref.md").write_text("# Ref\n", encoding="utf-8")
    skill = vendor / "e/.apm/skills/e-skill/SKILL.md"
    skill.write_text(
        skill.read_text(encoding="utf-8") + "See [ref](../../../docs/ref.md).\n",
        encoding="utf-8",
    )
    dump_yaml(
        {
            "name": "proj",
            "version": "1.0.0",
            "targets": ["claude"],
            "dependencies": {"apm": ["./vendor/e"]},
            "devDependencies": {"apm": ["./vendor/d"]},
        },
        world.project / "apm.yml",
    )
    (world.project / ".claude").mkdir()
    world.apm("install")
    deployed = (world.project / ".claude/skills/e-skill/SKILL.md").read_text(encoding="utf-8")
    assert "apm_modules/_local/e/docs/ref.md" in deployed, deployed
    before = (tree_digest(world.project), _modules(world.project))

    refused = world.apm("uninstall", "./vendor/e", expect=1)

    output = " ".join((refused.stdout + refused.stderr).split())
    assert "links into its apm_modules copy" in output, output
    assert (tree_digest(world.project), _modules(world.project)) == before


def test_a_reanchor_whose_project_skill_links_into_the_moving_copy_refuses(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """The project's own skill links e's docs through apm_modules: moving e dangles it."""
    from tests.integration._install_audit_replay_kit import tree_digest

    world = ReplayWorld.create(tmp_path, apm_binary_path)
    vendor = world.project / "vendor"
    _skill_package(vendor / "f", "f")
    _skill_package(vendor / "e", "e", ("../f",))
    _skill_package(vendor / "d", "d", ("../e",))
    (vendor / "e/docs").mkdir()
    (vendor / "e/docs/ref.md").write_text("# Ref\n", encoding="utf-8")
    own = world.project / ".apm/skills/proj-skill/SKILL.md"
    own.parent.mkdir(parents=True)
    own.write_text(
        "---\nname: proj-skill\ndescription: Project skill\n---\n"
        "See [ref](../../../apm_modules/_local/e/docs/ref.md).\n",
        encoding="utf-8",
    )
    dump_yaml(
        {
            "name": "proj",
            "version": "1.0.0",
            "targets": ["claude"],
            "dependencies": {"apm": ["./vendor/e"]},
            "devDependencies": {"apm": ["./vendor/d"]},
        },
        world.project / "apm.yml",
    )
    (world.project / ".claude").mkdir()
    world.apm("install")
    deployed = (world.project / ".claude/skills/proj-skill/SKILL.md").read_text(encoding="utf-8")
    assert "apm_modules/_local/e/docs/ref.md" in deployed, deployed
    before = (tree_digest(world.project), _modules(world.project))

    refused = world.apm("uninstall", "./vendor/e", expect=1)

    output = " ".join((refused.stdout + refused.stderr).split())
    assert "links into its apm_modules copy" in output, output
    assert (tree_digest(world.project), _modules(world.project)) == before


def _project_skill_world(tmp_path: Path, apm_binary_path: Path, body: str) -> ReplayWorld:
    """e -> f direct, dev d -> e, plus the project's own skill with *body*."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    vendor = world.project / "vendor"
    _skill_package(vendor / "f", "f")
    _skill_package(vendor / "e", "e", ("../f",))
    _skill_package(vendor / "d", "d", ("../e",))
    own = world.project / ".apm/skills/proj-skill/SKILL.md"
    own.parent.mkdir(parents=True)
    own.write_text(
        f"---\nname: proj-skill\ndescription: Project skill\n---\n{body}\n", encoding="utf-8"
    )
    dump_yaml(
        {
            "name": "proj",
            "version": "1.0.0",
            "targets": ["claude"],
            "dependencies": {"apm": ["./vendor/e"]},
            "devDependencies": {"apm": ["./vendor/d"]},
        },
        world.project / "apm.yml",
    )
    (world.project / ".claude").mkdir()
    world.apm("install")
    return world


def test_a_reanchor_whose_project_skill_links_the_moving_directory_refuses(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A link naming e's copy itself, with no trailing separator, dangles too."""
    from tests.integration._install_audit_replay_kit import tree_digest

    world = _project_skill_world(
        tmp_path, apm_binary_path, "See [package](../../../apm_modules/_local/e)."
    )
    deployed = (world.project / ".claude/skills/proj-skill/SKILL.md").read_text(encoding="utf-8")
    assert "apm_modules/_local/e)" in deployed, deployed
    before = (tree_digest(world.project), _modules(world.project))

    refused = world.apm("uninstall", "./vendor/e", expect=1)

    output = " ".join((refused.stdout + refused.stderr).split())
    assert "links into its apm_modules copy" in output, output
    assert (tree_digest(world.project), _modules(world.project)) == before


def test_a_reanchor_with_a_recorded_path_outside_the_project_refuses(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A surviving owner's ../ claim is never read: the re-anchor refuses before any write."""
    import yaml

    from tests.integration._install_audit_replay_kit import tree_digest

    world = _project_skill_world(tmp_path, apm_binary_path, "No links.")
    (tmp_path / "private.txt").write_text("secret\n", encoding="utf-8")
    lock_path = world.project / "apm.lock.yaml"
    lock = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    (row,) = (dep for dep in lock["dependencies"] if dep.get("local_path") == "./vendor/d")
    row.setdefault("deployed_files", []).append("../private.txt")
    dump_yaml(lock, lock_path)
    before = (tree_digest(world.project), _modules(world.project))

    refused = world.apm("uninstall", "./vendor/e", expect=1)

    output = " ".join((refused.stdout + refused.stderr).split())
    assert "../private.txt" in output, output
    assert (tree_digest(world.project), _modules(world.project)) == before


# -- A descendant the winning edge spells differently (#5091) --------------------


def _case_insensitive(path: Path) -> bool:
    probe = path / "CaseProbe"
    probe.mkdir()
    try:
        return (path / "caseprobe").exists()
    finally:
        probe.rmdir()


def _respelled_world(
    tmp_path: Path, apm_binary_path: Path, link_f, f_deps: tuple[str, ...] = (), prod_chain=False
) -> ReplayWorld:
    """e -> ../F direct, dev d -> e and a -> ../f; *link_f* makes vendor/f.

    *prod_chain* adds production p -> q -> ../F, which the walk reaches first
    although a's shallower dev edge wins F's spelling.
    """
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    vendor = world.project / "vendor"
    _skill_package(vendor / "F", "F", f_deps)
    if f_deps:
        _skill_package(vendor / "g", "g")
    if prod_chain:
        _skill_package(vendor / "q", "q", ("../F",))
        _skill_package(vendor / "p", "p", ("../q",))
    link_f(vendor)
    _skill_package(vendor / "e", "e", ("../F",))
    _skill_package(vendor / "d", "d", ("../e",))
    _skill_package(vendor / "a", "a", ("../f",))
    dump_yaml(
        {
            "name": "proj",
            "version": "1.0.0",
            "targets": ["claude"],
            "dependencies": {"apm": ["./vendor/e", *(["./vendor/p"] if prod_chain else [])]},
            "devDependencies": {"apm": ["./vendor/d", "./vendor/a"]},
        },
        world.project / "apm.yml",
    )
    (world.project / ".claude").mkdir()
    world.apm("install")
    return world


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize(
    ("f_deps", "prod_chain"),
    [((), False), (("../g",), False), (("../g",), True)],
    ids=["leaf", "with-child", "cross-root-child"],
)
def test_a_descendant_spelled_through_a_symlink_reanchors_to_the_fresh_spelling(
    tmp_path: Path, apm_binary_path: Path, f_deps: tuple[str, ...], prod_chain: bool
) -> None:
    """vendor/f links to vendor/F: a's ../f wins after e goes, so F takes that spelling;
    a child g beneath F then names F by that spelling too."""
    if _case_insensitive(tmp_path):
        pytest.skip("vendor/f and vendor/F are one name here; the case-only cell covers it")
    world = _respelled_world(
        tmp_path,
        apm_binary_path,
        lambda vendor: (vendor / "f").symlink_to(vendor / "F", target_is_directory=True),
        f_deps,
        prod_chain,
    )

    world.apm("uninstall", "./vendor/e")

    _assert_no_frozen_rewrites_nothing(world)


@pytest.mark.windows_compat
def test_a_case_only_descendant_spelling_reanchors_to_the_fresh_spelling(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """On a case-insensitive filesystem ../f and ../F are one package directory."""
    if not _case_insensitive(tmp_path):
        pytest.skip("needs a case-insensitive filesystem")
    world = _respelled_world(tmp_path, apm_binary_path, lambda vendor: None)

    world.apm("uninstall", "./vendor/e")

    _assert_no_frozen_rewrites_nothing(world)
    assert not [key for key in _modules(world.project) if ".apm-reanchor-" in key]


def test_two_packages_that_differ_only_by_case_stay_distinct(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """On a case-sensitive filesystem vendor/f and vendor/F are two packages."""
    if _case_insensitive(tmp_path):
        pytest.skip("needs a case-sensitive filesystem")
    # Skill names fold case, so the second package ships a distinct one.
    world = _respelled_world(
        tmp_path, apm_binary_path, lambda vendor: _skill_package(vendor / "f", "g")
    )

    world.apm("uninstall", "./vendor/e")

    copies = {key.rsplit("/", 2)[-2] for key in _modules(world.project) if key.endswith("/apm.yml")}
    assert {"F", "f"} <= copies, sorted(copies)
    _assert_no_frozen_rewrites_nothing(world)
