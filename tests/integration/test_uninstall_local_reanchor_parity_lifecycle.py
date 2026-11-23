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

import re
import sys
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.integration._install_audit_replay_kit import ReplayWorld, lock_bytes
from tests.lifecycle.known import KnownFailure

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
    # #5058: the stale copy is named, with the fix, before any write.
    assert re.search(r"differs from its apm_modules copy _local/(\S+/)?f,", output), output
    assert (
        "Run 'apm install --no-frozen' to recopy the edited local source into apm_modules, "
        "then retry the uninstall." in output
    ), output
    assert "Uninstall the package that still needs it first" not in output, output
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


# -- Open follow-ups, pinned as strict known cells (#5097, #5102) -------------------
#
# Each cell is red until its fix lands; the fix PR flips it to required.

_LINK = "apm_modules/_local/e/docs/ref.md"
_MANIFEST = {
    "name": "proj",
    "version": "1.0.0",
    "targets": ["claude"],
    "dependencies": {"apm": ["./vendor/e"]},
    "devDependencies": {"apm": ["./vendor/d"]},
}
_COMPILED_LINK = KnownFailure(
    "#5097",
    "a compiled AGENTS.md is no recorded claim: its link into the moving copy dangles",
    r"dangles after the re-anchor",
)
_LEDGER_ONLY_LINK = KnownFailure(
    "#5097",
    "a canonical ledger-only claim is outside the scanned view: its link dangles",
    r"dangles after the re-anchor",
)
_STALE_EXCLUDED = KnownFailure(
    "#5102",
    "the re-anchor moves a copy whose hash-excluded files went stale",
    r"the next install rewrote the re-anchored copy",
)


def _docs_world(tmp_path: Path, apm_binary_path: Path, extra=None) -> ReplayWorld:
    """e -> f direct, dev d -> e; e ships docs/ref.md; *extra* edits the tree first."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    vendor = world.project / "vendor"
    for name, deps in (("f", ()), ("e", ("../f",)), ("d", ("../e",))):
        _skill_package(vendor / name, name, deps)
    (vendor / "e/docs").mkdir()
    (vendor / "e/docs/ref.md").write_text("# Ref\n", encoding="utf-8")
    if extra is not None:
        extra(world.project)
    dump_yaml(_MANIFEST, world.project / "apm.yml")
    (world.project / ".claude").mkdir()
    world.apm("install")
    return world


def _assert_link_resolves(world: ReplayWorld, where: str) -> None:
    """The uninstall may keep the copy or refuse, but the link must still resolve.

    The file that names the link keeps its bytes: dropping the reference is no fix.
    """
    referencing = world.project / where
    kept = referencing.read_bytes()
    result = world.apm("uninstall", "./vendor/e", expect=None)
    output = " ".join((result.stdout + result.stderr).split())
    assert result.returncode in (0, 1) and "Traceback" not in output, output
    assert referencing.is_file() and referencing.read_bytes() == kept, (
        f"the uninstall rewrote or removed {where}, which names {_LINK}\n{output}"
    )
    assert (world.project / _LINK).is_file(), (
        f"{where} names {_LINK}, which dangles after the re-anchor\n{output}"
    )


@_COMPILED_LINK.mark()
def test_a_compiled_agents_md_link_into_the_moving_copy_keeps_resolving(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#5097 F1: the project's always-on instruction links e's docs in its copy; compile
    writes the link into AGENTS.md, which is no recorded claim the re-anchor scans."""

    def instruction(project: Path) -> None:
        rules = project / ".apm/instructions/proj.instructions.md"
        rules.parent.mkdir(parents=True)
        rules.write_text(
            f'---\napplyTo: "**"\ndescription: Project rules\n---\nSee [ref]({_LINK}).\n',
            encoding="utf-8",
        )

    world = _docs_world(tmp_path, apm_binary_path, instruction)
    world.apm("compile")
    agents = (world.project / "AGENTS.md").read_text(encoding="utf-8")
    assert f"]({_LINK})" in agents, agents
    assert (world.project / _LINK).is_file()

    _assert_link_resolves(world, "AGENTS.md")


@_LEDGER_ONLY_LINK.mark()
def test_a_ledger_only_claim_linking_into_the_moving_copy_keeps_resolving(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#5097 F2: the project skill's claim lives only in the canonical ``deployments`` rows."""
    import yaml

    def own_skill(project: Path) -> None:
        own = project / ".apm/skills/proj-skill/SKILL.md"
        own.parent.mkdir(parents=True)
        own.write_text(
            "---\nname: proj-skill\ndescription: Project skill\n---\n"
            f"See [ref](../../../{_LINK}).\n",
            encoding="utf-8",
        )

    world = _docs_world(tmp_path, apm_binary_path, own_skill)
    skill = ".claude/skills/proj-skill"
    assert _LINK in (world.project / skill / "SKILL.md").read_text(encoding="utf-8")
    lock_path = world.project / "apm.lock.yaml"
    lock = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    rows = [row["value"] for row in lock.get("deployments") or ()]
    assert any(value.startswith(skill) for value in rows), rows
    legacy = lock.get("local_deployed_files") or []
    lock["local_deployed_files"] = [path for path in legacy if not path.startswith(skill)]
    assert lock["local_deployed_files"] != legacy, legacy
    dump_yaml(lock, lock_path)

    _assert_link_resolves(world, skill + "/SKILL.md")


@_STALE_EXCLUDED.mark()
def test_a_reanchored_copy_whose_excluded_files_changed_needs_no_rewrite(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#5102: e's .git/HEAD (hash-excluded) changes after install; e re-anchors."""

    def git_head(project: Path) -> None:
        head = project / "vendor/e/.git/HEAD"
        head.parent.mkdir(parents=True)
        head.write_text("ref: refs/heads/main\n", encoding="utf-8")

    world = _docs_world(tmp_path, apm_binary_path, git_head)
    (world.project / "vendor/e/.git/HEAD").write_text("ref: refs/heads/other\n", encoding="utf-8")

    world.apm("uninstall", "./vendor/e")
    lock, modules = lock_bytes(world.project), _modules(world.project)
    installed = world.apm("install", "--no-frozen")

    assert installed.returncode == 0, installed.stdout + installed.stderr
    after = _modules(world.project)
    changed = sorted(
        key for key in modules.keys() | after.keys() if modules.get(key) != after.get(key)
    )
    assert lock_bytes(world.project) == lock, "the next install rewrote the lockfile"
    assert not changed, f"the next install rewrote the re-anchored copy: {changed}"


# -- A descendant the winning edge spells differently (#5091) --------------------


def _case_insensitive(path: Path) -> bool:
    probe = path / "CaseProbe"
    probe.mkdir()
    try:
        return (path / "caseprobe").exists()
    finally:
        probe.rmdir()


def _respelled_world(
    tmp_path: Path,
    apm_binary_path: Path,
    link_f,
    f_deps: tuple[str, ...] = (),
    prod_chain=False,
    other_f=False,
) -> ReplayWorld:
    """e -> ../F direct, dev d -> e and a -> ../f; *link_f* makes vendor/f.

    *prod_chain* adds production p -> q -> ../F, which the walk reaches first
    although a's shallower dev edge wins F's spelling. *other_f* adds an
    unrelated direct ./other/F -> ../h, whose parent identity is also _local/F.
    """
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    vendor = world.project / "vendor"
    _skill_package(vendor / "F", "F", f_deps)
    if f_deps:
        _skill_package(vendor / "g", "g")
    if prod_chain:
        _skill_package(vendor / "q", "q", ("../F",))
        _skill_package(vendor / "p", "p", ("../q",))
    if other_f:
        _skill_package(world.project / "other/F", "o", ("../h",))
        _skill_package(world.project / "other/h", "h")
    link_f(vendor)
    _skill_package(vendor / "e", "e", ("../F",))
    _skill_package(vendor / "d", "d", ("../e",))
    _skill_package(vendor / "a", "a", ("../f",))
    dump_yaml(
        {
            "name": "proj",
            "version": "1.0.0",
            "targets": ["claude"],
            "dependencies": {
                "apm": [
                    "./vendor/e",
                    *(["./vendor/p"] if prod_chain else []),
                    *(["./other/F"] if other_f else []),
                ]
            },
            "devDependencies": {"apm": ["./vendor/d", "./vendor/a"]},
        },
        world.project / "apm.yml",
    )
    (world.project / ".claude").mkdir()
    world.apm("install")
    return world


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize(
    ("f_deps", "prod_chain", "other_f"),
    [
        ((), False, False),
        (("../g",), False, False),
        (("../g",), True, False),
        (("../g",), True, True),
    ],
    ids=["leaf", "with-child", "cross-root-child", "unrelated-same-name-parent"],
)
def test_a_descendant_spelled_through_a_symlink_reanchors_to_the_fresh_spelling(
    tmp_path: Path, apm_binary_path: Path, f_deps: tuple[str, ...], prod_chain: bool, other_f
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
        other_f,
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
    assert not list((world.project / "apm_modules").rglob(".apm-case-*"))


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
