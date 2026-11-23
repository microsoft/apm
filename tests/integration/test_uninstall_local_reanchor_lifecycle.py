"""Uninstall keeps a still-needed LOCAL dependency by re-anchoring it (#4889).

PROMISE: P-INV-I3 -- after ``apm uninstall ./vendor/e``, where a surviving
package still declares ``e``, ``e`` stays installed under the identity
``apm install --no-frozen`` records for it (its lock row, ledger owners and
deployment receipts are re-keyed), so ``apm install --frozen`` passes and
later removals keep deletion evidence (source: #4889 scope record).
CELL: OP=uninstall SRC=local PRIM=skill SCOPE=project LOCK=consistent
GRAPH=direct-also-transitive | refusals: forged receipt, offline-unreadable graph

Resolution is the offline reachability walk; a re-anchor never fetches and
an incomplete walk, or a receipt APM cannot prove, refuses before any write.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._install_audit_replay_kit import (
    ReplayWorld,
    lock_bytes,
    tree_digest,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I3", "P-INV-I1", "P-INV-I2"),
]


def write(path: Path, text: str) -> None:
    """UTF-8, so a re-anchored path may be non-ASCII."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _skill_package(root: Path, name: str, deps: tuple[str, ...] = (), body: str = "") -> None:
    manifest = f"name: {name}\nversion: 1.0.0\n"
    if deps:
        manifest += "dependencies:\n  apm:\n" + "".join(f"    - {dep}\n" for dep in deps)
    write(root / "apm.yml", manifest)
    write(
        root / f".apm/skills/{name}-skill/SKILL.md",
        f"---\nname: {name}-skill\ndescription: Skill of {name}\n---\n# {name}{body}\n",
    )


def _manifest(world: ReplayWorld, direct: list, dev: list) -> None:
    dump_yaml(
        {
            "name": "proj",
            "version": "1.0.0",
            "targets": ["claude"],
            "dependencies": {"apm": direct},
            "devDependencies": {"apm": dev},
        },
        world.project / "apm.yml",
    )


def _local_world(tmp_path: Path, apm_binary_path: Path, e_dir: str = "e") -> ReplayWorld:
    """Graph local e -> f, dev d -> e, dev a -> f, with e declared direct (#4889)."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    vendor = world.project / "vendor"
    _skill_package(vendor / "f", "f")
    _skill_package(vendor / e_dir, "e", ("../f",))
    _skill_package(vendor / "d", "d", (f"../{e_dir}",))
    _skill_package(vendor / "a", "a", ("../f",))
    _manifest(world, [f"./vendor/{e_dir}"], ["./vendor/d", "./vendor/a"])
    (world.project / ".claude").mkdir()
    world.apm("install")
    return world


def _receipts(world: ReplayWorld) -> dict[str, bytes]:
    directory = world.isolated.home / ".apm" / "deployment-receipts"
    return {path.name: path.read_bytes() for path in sorted(directory.glob("*.json"))}


def _rows(world: ReplayWorld) -> dict[str, dict]:
    lock = load_yaml(world.project / "apm.lock.yaml")
    return {row["repo_url"]: row for row in lock["dependencies"]}


def _assert_reanchored_like_a_full_install(world: ReplayWorld) -> None:
    """Done-when 1: frozen passes; a REAL --no-frozen rewrites neither lock nor receipts."""
    world.apm("install", "--frozen")
    lock, receipts = lock_bytes(world.project), _receipts(world)
    world.apm("install", "--no-frozen")
    assert lock_bytes(world.project) == lock
    assert _receipts(world) == receipts


def _assert_refused_before_any_write(world: ReplayWorld, *expected: str) -> None:
    before, receipts = tree_digest(world.project), _receipts(world)
    refused = world.apm("uninstall", "./vendor/e", expect=1)
    output = " ".join((refused.stdout + refused.stderr).split())
    for text in expected:
        assert text in output, output
    assert "apm.yml and apm.lock.yaml were not changed" in output, output
    assert "Traceback" not in output, output
    assert tree_digest(world.project) == before
    assert _receipts(world) == receipts


def test_uninstall_reanchors_a_still_needed_local_dependency(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4889 cell 1: e stays under d's declaration; frozen passes, no-frozen rewrites nothing."""
    world = _local_world(tmp_path, apm_binary_path)

    result = world.apm("uninstall", "./vendor/e")

    output = " ".join((result.stdout + result.stderr).split())
    assert "./vendor/e stays installed because" in output, output
    row = _rows(world)["_local/e"]
    assert (row["local_path"], row["declaring_parent"], row["anchored_local_path"]) == (
        "../e",
        "./vendor/d",
        "vendor/e",
    )
    assert row.get("is_dev") is True
    assert (world.project / ".claude/skills/e-skill/SKILL.md").is_file()
    _assert_reanchored_like_a_full_install(world)
    world.audit_clean()


def test_later_uninstall_and_prune_of_the_reanchored_package_keep_deletion_evidence(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4889 cell 2: removing d later deletes e's proven files and keeps an edited one."""
    world = _local_world(tmp_path, apm_binary_path)
    world.apm("uninstall", "./vendor/e")
    edited = world.project / ".claude/skills/e-skill/SKILL.md"
    edited.write_text(edited.read_text(encoding="utf-8") + "\nmine\n", encoding="utf-8")

    before = tree_digest(world.project)

    # The edit is evidence that APM did not write those bytes: the removal
    # refuses, names the file and changes nothing (#3701).
    refused = world.apm("uninstall", "./vendor/d", expect=1)

    output = " ".join((refused.stdout + refused.stderr).split())
    assert ".claude/skills/e-skill/SKILL.md: edited since APM deployed it" in output, output
    assert tree_digest(world.project) == before
    # Once the edit is moved aside, the proven files go with the package.
    edited.unlink()
    world.apm("uninstall", "./vendor/d")
    assert "_local/e" not in _rows(world)
    assert not (world.project / ".claude/skills/d-skill").exists()

    # Prune deletes only what it proves: the re-anchored package keeps its
    # files, row and receipts (prune of a transitive local orphan: #4989).
    other = _local_world(tmp_path / "prune", apm_binary_path)
    other.apm("uninstall", "./vendor/e")
    _manifest(other, [], ["./vendor/a"])
    receipts = _receipts(other)
    pruned = other.apm("prune")
    assert "Removed _local/d" in pruned.stdout + pruned.stderr
    assert not (other.project / ".claude/skills/d-skill").exists()
    assert (other.project / ".claude/skills/e-skill/SKILL.md").is_file()
    assert "_local/e" in _rows(other)
    assert _receipts(other).keys() == receipts.keys()


def test_a_forged_receipt_binding_refuses_the_reanchor(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4889 cell 3: a receipt bound to another owner refuses before any write (I7)."""
    world = _local_world(tmp_path, apm_binary_path)
    directory = world.isolated.home / ".apm" / "deployment-receipts"
    (receipt,) = [
        path for path in directory.glob("*.json") if "e-skill" in path.read_text(encoding="utf-8")
    ]
    data = json.loads(receipt.read_text(encoding="utf-8"))
    for key, binding in data["bindings"].items():
        if "e-skill" in key:
            binding[0] = "./vendor/x"
    receipt.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    _assert_refused_before_any_write(world, "APM cannot prove it wrote", "e-skill")


def test_a_reanchor_that_would_need_the_network_refuses(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4889 cell 4: a survivor's remote dependency missing offline refuses; nothing changes."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    world.publish("r", lambda root, name: _skill_package(root, name))
    vendor = world.project / "vendor"
    _skill_package(vendor / "e", "e")
    _skill_package(vendor / "d", "d", ("../e", "test/r"))
    _manifest(world, ["./vendor/e"], ["./vendor/d"])
    (world.project / ".claude").mkdir()
    world.apm("install")
    shutil.rmtree(world.project / "apm_modules" / "test" / "r")

    _assert_refused_before_any_write(world, "could not all be read offline", "never fetches")


def test_an_edit_made_before_reanchoring_is_still_reported(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4889 cell 6: re-keying keeps edit evidence; audit still reports the edited file."""
    world = _local_world(tmp_path, apm_binary_path)
    edited = world.project / ".claude/skills/e-skill/SKILL.md"
    edited.write_text(edited.read_text(encoding="utf-8") + "\nmine\n", encoding="utf-8")

    world.apm("uninstall", "./vendor/e")

    assert edited.read_text(encoding="utf-8").endswith("mine\n")
    audit = world.apm("audit", "--ci", "--no-policy", "--no-fail-fast", expect=None)
    output = " ".join((audit.stdout + audit.stderr).split())
    assert ".claude/skills/e-skill/SKILL.md [local:vendor/e]" in output, output


def test_reanchoring_carries_the_receipt_hash_history(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4889 cell 7: the owner rename never forgets a receipt's earlier hashes (#4428)."""
    world = _local_world(tmp_path, apm_binary_path)
    _skill_package(world.project / "vendor" / "e", "e", ("../f",), body=" v2")
    world.apm("install")

    def history() -> dict:
        merged: dict = {}
        for raw in _receipts(world).values():
            merged.update(json.loads(raw).get("history") or {})
        return {key: value for key, value in merged.items() if "e-skill" in key}

    before = history()
    assert before, "the rewrite recorded no receipt history"

    result = world.apm("uninstall", "./vendor/e")

    assert "./vendor/e stays installed because" in result.stdout + result.stderr
    assert _rows(world)["_local/e"]["declaring_parent"] == "./vendor/d"
    assert history() == before


@pytest.mark.parametrize("e_dir", ["my e", "caf\u00e9-e", "E-Pkg", "cafe\u0301-e"])
def test_reanchor_keeps_whitespace_case_and_unicode_local_paths(
    tmp_path: Path, apm_binary_path: Path, e_dir: str
) -> None:
    """#4889 cells 8a/8d: whitespace, non-ASCII, mixed-case and NFD paths keep one identity."""
    world = _local_world(tmp_path, apm_binary_path, e_dir=e_dir)
    world.apm("uninstall", f"./vendor/{e_dir}")
    _assert_reanchored_like_a_full_install(world)


def test_reanchor_refuses_when_the_kept_package_directory_is_gone(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4889 cell 8b: a missing local target never falls back to a parent directory."""
    world = _local_world(tmp_path, apm_binary_path)
    shutil.rmtree(world.project / "vendor" / "f")

    _assert_refused_before_any_write(world, "could not all be read offline")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_reanchor_through_a_symlinked_local_directory(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4889 cell 8c: a symlinked dependency directory keeps the identity install records."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    outside = world.isolated.work_root / "outside-e"
    _skill_package(outside, "e")
    vendor = world.project / "vendor"
    vendor.mkdir()
    os.symlink(outside, vendor / "e", target_is_directory=True)
    _skill_package(vendor / "d", "d", ("../e",))
    _manifest(world, ["./vendor/e"], ["./vendor/d"])
    (world.project / ".claude").mkdir()
    world.apm("install")

    world.apm("uninstall", "./vendor/e")

    _assert_reanchored_like_a_full_install(world)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_reanchor_refuses_a_symlink_loop(tmp_path: Path, apm_binary_path: Path) -> None:
    """#4889 cell 8c: a looping dependency directory refuses cleanly, never hangs or crashes."""
    world = _local_world(tmp_path, apm_binary_path)
    loop = world.project / "vendor" / "f"
    shutil.rmtree(loop)
    os.symlink(loop, loop, target_is_directory=True)

    _assert_refused_before_any_write(world, "could not all be read offline")


def test_reanchor_of_a_dependency_outside_the_project(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4889 cell 8e: ``..`` and absolute local paths re-anchor to the identity install records."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    shared = world.isolated.work_root / "shared"
    _skill_package(shared / "e", "e")
    _skill_package(world.project / "vendor" / "d", "d", (str(shared / "e"),))
    _manifest(world, ["../shared/e"], ["./vendor/d"])
    (world.project / ".claude").mkdir()
    world.apm("install")

    world.apm("uninstall", "../shared/e")

    _assert_reanchored_like_a_full_install(world)


_REPLACEMENTS_SCHEMA = (
    "https://microsoft.github.io/apm/specs/schemas/manifest-source-replacements-v1.schema.json"
)


def test_reanchor_rekeys_replacement_request_parents(tmp_path: Path, apm_binary_path: Path) -> None:
    """#4889 cell 5: e's edge into a replaced package names e's new key (req-rpl-003)."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    vendor = world.project / "vendor"
    _skill_package(vendor / "r", "r")
    _skill_package(vendor / "e", "e", ("test/r",))
    _skill_package(vendor / "d", "d", ("../e",))
    dump_yaml(
        {
            "$schema": _REPLACEMENTS_SCHEMA,
            "name": "proj",
            "version": "1.0.0",
            "targets": ["claude"],
            "dependencies": {"apm": ["./vendor/e"]},
            "devDependencies": {"apm": ["./vendor/d"]},
            "replace": [
                {"for": {"git": "https://github.com/test/r"}, "with": {"path": "./vendor/r"}}
            ],
        },
        world.project / "apm.yml",
    )
    (world.project / ".claude").mkdir()
    world.apm("install")

    world.apm("uninstall", "./vendor/e")

    lock = load_yaml(world.project / "apm.lock.yaml")
    parents = [
        request["parent"]
        for row in lock["dependencies"]
        for request in (row.get("replacement") or {}).get("requests") or []
    ]
    assert parents == ["local:vendor/e"], lock
    _assert_reanchored_like_a_full_install(world)


@pytest.mark.windows_compat
def test_reanchor_with_a_case_variant_parent_spelling(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4889 cell 8d: on a case-insensitive filesystem d's ``../e-pkg`` is root's ``E-Pkg``."""
    probe = tmp_path / "Case-Probe"
    probe.mkdir()
    if not (tmp_path / "case-probe").exists():
        pytest.skip("case-sensitive filesystem: the spellings name different directories")
    world = ReplayWorld.create(tmp_path / "w2", apm_binary_path)
    vendor = world.project / "vendor"
    _skill_package(vendor / "E-Pkg", "e")
    _skill_package(vendor / "d", "d", ("../e-pkg",))
    _manifest(world, ["./vendor/E-Pkg"], ["./vendor/d"])
    (world.project / ".claude").mkdir()
    world.apm("install")

    world.apm("uninstall", "./vendor/E-Pkg")

    _assert_reanchored_like_a_full_install(world)


@pytest.mark.skipif(
    sys.platform == "win32" or not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="needs POSIX permissions enforced for a non-root user",
)
def test_a_failed_receipt_rename_rolls_the_uninstall_back(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4889: one transaction -- receipts that cannot move restore apm.yml and the lock."""
    world = _local_world(tmp_path, apm_binary_path)
    directory = world.isolated.home / ".apm" / "deployment-receipts"
    manifest, lock, receipts = (
        (world.project / "apm.yml").read_bytes(),
        lock_bytes(world.project),
        _receipts(world),
    )
    directory.chmod(0o500)
    try:
        result = world.apm("uninstall", "./vendor/e", expect=1)
    finally:
        directory.chmod(0o700)

    output = " ".join((result.stdout + result.stderr).split())
    assert "apm.yml and apm.lock.yaml were restored" in output, output
    assert (world.project / "apm.yml").read_bytes() == manifest
    assert lock_bytes(world.project) == lock
    assert _receipts(world) == receipts
    world.apm("install", "--frozen")


def _graph(tmp_path: Path, apm_binary_path: Path, packages, dev: list[str]) -> ReplayWorld:
    """Local ``(name, deps)`` packages under vendor/, e direct, *dev* as devDependencies."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    for name, deps in packages:
        _skill_package(world.project / "vendor" / name, name, deps)
    _manifest(world, ["./vendor/e"], dev)
    (world.project / ".claude").mkdir()
    world.apm("install")
    return world


def test_reanchor_keeps_the_first_declaring_edge(tmp_path: Path, apm_binary_path: Path) -> None:
    """#4889: z then a both declare ../e; install keeps z's edge, ties resolved_by to a."""
    world = _graph(
        tmp_path,
        apm_binary_path,
        (("e", ()), ("z", ("../e",)), ("a", ("../e",))),
        ["./vendor/z", "./vendor/a"],
    )
    world.apm("uninstall", "./vendor/e")
    row = _rows(world)["_local/e"]
    assert (row["declaring_parent"], row["resolved_by"]) == ("./vendor/z", "_local/a")
    _assert_reanchored_like_a_full_install(world)


def test_reanchor_ranks_an_equal_depth_diamond_by_the_new_keys(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4889: d -> e -> f and b -> a -> f; once e is transitive, a's key ranks first."""
    world = _graph(
        tmp_path,
        apm_binary_path,
        (("f", ()), ("e", ("../f",)), ("a", ("../f",)), ("d", ("../e",)), ("b", ("../a",))),
        ["./vendor/d", "./vendor/b"],
    )
    world.apm("uninstall", "./vendor/e")
    assert _rows(world)["_local/f"]["resolved_by"] == "_local/a"
    _assert_reanchored_like_a_full_install(world)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_reanchor_refuses_an_alias_spelling_that_installs_elsewhere(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4889: d declares ../e-link (a symlink to e); install would record _local/e-link."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    vendor = world.project / "vendor"
    _skill_package(vendor / "e", "e")
    os.symlink("e", vendor / "e-link", target_is_directory=True)
    _skill_package(vendor / "d", "d", ("../e-link",))
    _manifest(world, ["./vendor/e"], ["./vendor/d"])
    (world.project / ".claude").mkdir()
    world.apm("install")

    _assert_refused_before_any_write(world, "declares it as _local/e-link")


def test_a_reanchor_mixed_with_another_removal_refuses(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4889: a re-anchor rolls back only apm.yml and the lock, so it never runs with a deletion."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    vendor = world.project / "vendor"
    _skill_package(vendor / "e", "e")
    _skill_package(vendor / "r", "r")
    _skill_package(vendor / "d", "d", ("../e",))
    _manifest(world, ["./vendor/e", "./vendor/r"], ["./vendor/d"])
    (world.project / ".claude").mkdir()
    world.apm("install")
    before, receipts = tree_digest(world.project), _receipts(world)

    refused = world.apm("uninstall", "./vendor/e", "./vendor/r", expect=1)

    output = " ".join((refused.stdout + refused.stderr).split())
    assert "uninstall ./vendor/r in a separate command" in output, output
    assert tree_digest(world.project) == before
    assert _receipts(world) == receipts
    world.apm("uninstall", "./vendor/r")
    world.apm("uninstall", "./vendor/e")
    _assert_reanchored_like_a_full_install(world)


def test_a_local_removal_without_apm_modules_refuses_when_a_local_survivor_could_need_it(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4889: with apm_modules/ gone the still-needed split cannot run; refuse before writes."""
    world = _local_world(tmp_path, apm_binary_path)
    shutil.rmtree(world.project / "apm_modules")

    _assert_refused_before_any_write(world, "apm_modules/ is missing")


def test_a_reanchor_whose_survivor_selects_fewer_skills_refuses(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4889: a narrower surviving selection would delete deployed skills; refuse first."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    vendor = world.project / "vendor"
    _skill_package(vendor / "e", "e")
    write(
        vendor / "e/.apm/skills/extra-skill/SKILL.md",
        "---\nname: extra-skill\ndescription: x\n---\n# x\n",
    )
    write(
        vendor / "d/apm.yml",
        "name: d\nversion: 1.0.0\ndependencies:\n  apm:\n"
        "    - path: ../e\n      skills: [e-skill]\n",
    )
    write(
        vendor / "d/.apm/skills/d-skill/SKILL.md", "---\nname: d-skill\ndescription: d\n---\n# d\n"
    )
    _manifest(world, ["./vendor/e"], ["./vendor/d"])
    (world.project / ".claude").mkdir()
    world.apm("install")
    assert (world.project / ".claude/skills/extra-skill/SKILL.md").is_file()

    _assert_refused_before_any_write(world, "select fewer skills or targets of ./vendor/e")


def test_a_reanchor_under_another_alias_refuses(tmp_path: Path, apm_binary_path: Path) -> None:
    """#4889: a surviving declaration with another alias installs elsewhere; refuse first."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    vendor = world.project / "vendor"
    _skill_package(vendor / "e", "e")
    write(
        vendor / "d/apm.yml",
        "name: d\nversion: 1.0.0\ndependencies:\n  apm:\n    - path: ../e\n      alias: new-e\n",
    )
    write(
        vendor / "d/.apm/skills/d-skill/SKILL.md", "---\nname: d-skill\ndescription: d\n---\n# d\n"
    )
    _manifest(world, ["./vendor/e"], ["./vendor/d"])
    (world.project / ".claude").mkdir()
    world.apm("install")

    _assert_refused_before_any_write(world, "under another name (new-e)")


@pytest.mark.parametrize("receipt", ["forged", "missing"])
def test_a_file_claim_without_a_lock_hash_still_needs_its_receipt(
    tmp_path: Path, apm_binary_path: Path, receipt: str
) -> None:
    """#4889: a null lock hash never exempts a file; a forged or missing receipt refuses."""
    world = _local_world(tmp_path, apm_binary_path)
    claim = ".claude/skills/e-skill/SKILL.md"
    lock_path = world.project / "apm.lock.yaml"
    lock = load_yaml(lock_path)
    for row in lock["dependencies"]:
        (row.get("deployed_file_hashes") or {}).pop(claim, None)
    for record in lock.get("deployments") or []:
        if record["value"] == claim:
            record["content_hash"] = None
    dump_yaml(lock, lock_path)
    directory = world.isolated.home / ".apm" / "deployment-receipts"
    (path,) = [p for p in directory.glob("*.json") if claim in p.read_text(encoding="utf-8")]
    data = json.loads(path.read_text(encoding="utf-8"))
    (key,) = [key for key in data["bindings"] if key.endswith(claim)]
    if receipt == "forged":
        data["bindings"][key][0] = "./vendor/x"
    else:
        for section in ("bindings", "destinations", "history"):
            (data.get(section) or {}).pop(key, None)
        data["records"] = [r for r in data["records"] if r["value"] != claim]
    path.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    _assert_refused_before_any_write(world, "APM cannot prove it wrote", claim)


def test_a_reparented_replacement_request_takes_the_new_dev_status(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4889: e -> f -> replaced r, dev d -> e; once e is not direct, f's request is dev."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    vendor = world.project / "vendor"
    _skill_package(vendor / "r", "r")
    _skill_package(vendor / "f", "f", ("test/r",))
    _skill_package(vendor / "e", "e", ("../f",))
    _skill_package(vendor / "d", "d", ("../e",))
    dump_yaml(
        {
            "$schema": _REPLACEMENTS_SCHEMA,
            "name": "proj",
            "version": "1.0.0",
            "targets": ["claude"],
            "dependencies": {"apm": ["./vendor/e"]},
            "devDependencies": {"apm": ["./vendor/d"]},
            "replace": [
                {"for": {"git": "https://github.com/test/r"}, "with": {"path": "./vendor/r"}}
            ],
        },
        world.project / "apm.yml",
    )
    (world.project / ".claude").mkdir()
    world.apm("install")

    world.apm("uninstall", "./vendor/e")

    lock = load_yaml(world.project / "apm.lock.yaml")
    requests = [
        request
        for row in lock["dependencies"]
        for request in (row.get("replacement") or {}).get("requests") or []
    ]
    assert [request["dev"] for request in requests] == [True], lock
    _assert_reanchored_like_a_full_install(world)


def test_a_stale_receipt_outside_the_owner_transition_is_untouched(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4889: only receipts the validated rename covers move; a stale one keeps its bytes."""
    world = _local_world(tmp_path, apm_binary_path)
    directory = world.isolated.home / ".apm" / "deployment-receipts"
    (path,) = [p for p in directory.glob("*.json") if "f-skill" in p.read_text(encoding="utf-8")]
    data = json.loads(path.read_text(encoding="utf-8"))
    (key,) = [key for key in data["bindings"] if key.endswith(".claude/skills/f-skill/SKILL.md")]
    for record in data["records"]:
        if record["value"] == ".claude/skills/f-skill/SKILL.md":
            record["owners"], record["active_owner"] = ["./vendor/e"], "./vendor/e"
    data["bindings"][key][0] = "./vendor/e"
    path.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    stale = (data["bindings"][key], data["destinations"][key])

    world.apm("uninstall", "./vendor/e")

    after = json.loads(path.read_text(encoding="utf-8"))
    assert (after["bindings"][key], after["destinations"][key]) == stale
    (record,) = [r for r in after["records"] if r["value"] == ".claude/skills/f-skill/SKILL.md"]
    assert record["active_owner"] == "./vendor/e"


def test_without_apm_modules_an_unreached_local_removal_still_runs(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4889: the offline refusal needs a surviving local source that reaches the package."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    vendor = world.project / "vendor"
    _skill_package(vendor / "e", "e")
    _skill_package(vendor / "x", "x")
    _manifest(world, ["./vendor/e", "./vendor/x"], [])
    (world.project / ".claude").mkdir()
    world.apm("install")
    shutil.rmtree(world.project / "apm_modules")

    preview = world.apm("uninstall", "./vendor/e", "--dry-run")
    result = world.apm("uninstall", "./vendor/e")

    assert "apm_modules/ is missing" not in preview.stdout + preview.stderr
    assert "_local/e" not in _rows(world)
    assert "Removed ./vendor/e" in result.stdout + result.stderr


def test_without_apm_modules_an_unreadable_survivor_source_refuses(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4889: a survivor directory that is not a leaf and has no manifest proves nothing."""
    world = _local_world(tmp_path, apm_binary_path)
    shutil.rmtree(world.project / "apm_modules")
    (world.project / "vendor" / "d" / "apm.yml").unlink()

    _assert_refused_before_any_write(world, "apm_modules/ is missing")


@pytest.mark.windows_compat
def test_without_apm_modules_a_case_variant_survivor_edge_refuses(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4889: on a case-insensitive filesystem d's ../E reaches ./vendor/e by identity."""
    probe = tmp_path / "Case-Probe"
    probe.mkdir()
    if not (tmp_path / "case-probe").exists():
        pytest.skip("case-sensitive filesystem: the spellings name different directories")
    world = _local_world(tmp_path / "w2", apm_binary_path)
    _skill_package(world.project / "vendor" / "d", "d", ("../E",))
    shutil.rmtree(world.project / "apm_modules")

    _assert_refused_before_any_write(world, "apm_modules/ is missing")


def test_a_reanchor_refuses_when_a_descendant_selection_shrinks(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4889: any survivor whose selection shrinks would lose files; the re-anchor refuses."""
    world = _local_world(tmp_path, apm_binary_path)
    vendor = world.project / "vendor"
    write(
        vendor / "f/.apm/skills/g-skill/SKILL.md", "---\nname: g-skill\ndescription: g\n---\n# g\n"
    )
    world.apm("install")
    assert (world.project / ".claude/skills/g-skill/SKILL.md").is_file()
    write(
        vendor / "e/apm.yml",
        "name: e\nversion: 1.0.0\ndependencies:\n  apm:\n    - path: ../f\n      skills: [f-skill]\n",
    )
    write(vendor / "a/apm.yml", "name: a\nversion: 1.0.0\n")

    _assert_refused_before_any_write(world, "select fewer skills or targets of")


def _modules_digest(world: ReplayWorld) -> dict[str, bytes]:
    root = world.project / "apm_modules"
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_a_reanchor_refuses_when_a_local_source_changed_since_install(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4889: re-copying an edited source cannot be rolled back; refuse before activation."""
    world = _local_world(tmp_path, apm_binary_path)
    _skill_package(world.project / "vendor" / "e", "e", ("../f",), body=" edited")
    modules = _modules_digest(world)

    _assert_refused_before_any_write(world, "changed since it was installed")
    assert _modules_digest(world) == modules


def test_without_apm_modules_a_missing_survivor_source_refuses(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4889: a surviving local source that is gone proves nothing; it never counts as a leaf."""
    world = _local_world(tmp_path, apm_binary_path)
    shutil.rmtree(world.project / "apm_modules")
    shutil.rmtree(world.project / "vendor" / "d")

    _assert_refused_before_any_write(world, "apm_modules/ is missing")


def _without_modules(world: ReplayWorld) -> None:
    shutil.rmtree(world.project / "apm_modules")


def test_a_dry_run_previews_the_offline_refusal_as_kept(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4889: same reaching shape -- the preview keeps the package; the real run refuses."""
    world = _local_world(tmp_path, apm_binary_path)
    _without_modules(world)
    before, receipts = tree_digest(world.project), _receipts(world)

    preview = world.apm("uninstall", "./vendor/e", "--dry-run")

    output = " ".join((preview.stdout + preview.stderr).split())
    assert "Dry run: Would keep ./vendor/e: apm_modules/ is missing" in output, output
    assert output.count("Would keep ./vendor/e") == 1, output
    assert "Would remove ./vendor/e" not in output, output
    assert "./vendor/e from apm.yml" not in output, output
    assert tree_digest(world.project) == before
    assert _receipts(world) == receipts
    _assert_refused_before_any_write(world, "apm_modules/ is missing")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_without_apm_modules_a_symlinked_survivor_directory_refuses(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4889: a survivor reached through a symlinked directory still reaches e by identity."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    vendor = world.project / "vendor"
    _skill_package(vendor / "e", "e")
    outside = world.isolated.work_root / "outside-d"
    _skill_package(outside, "d", (str(vendor / "e"),))
    vendor.mkdir(exist_ok=True)
    os.symlink(outside, vendor / "d", target_is_directory=True)
    _manifest(world, ["./vendor/e"], ["./vendor/d"])
    (world.project / ".claude").mkdir()
    world.apm("install")
    _without_modules(world)

    _assert_refused_before_any_write(world, "apm_modules/ is missing")


def test_without_apm_modules_a_survivor_outside_the_project_refuses(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4889: a ``..`` survivor that declares ../proj/vendor/e reaches it."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    _skill_package(world.project / "vendor" / "e", "e")
    shared = world.isolated.work_root / "shared"
    _skill_package(shared / "d", "d", (f"../../{world.project.name}/vendor/e",))
    _manifest(world, ["./vendor/e"], ["../shared/d"])
    (world.project / ".claude").mkdir()
    world.apm("install")
    _without_modules(world)

    _assert_refused_before_any_write(world, "apm_modules/ is missing")


def test_without_apm_modules_a_plugin_format_survivor_refuses(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4889: a Claude plugin survivor is read as install normalizes it, never as a leaf."""
    world = _local_world(tmp_path, apm_binary_path)
    _without_modules(world)
    survivor = world.project / "vendor" / "d"
    (survivor / "apm.yml").unlink()
    write(
        survivor / ".claude-plugin" / "plugin.json",
        json.dumps({"name": "d", "version": "1.0.0", "dependencies": ["../e"]}),
    )

    _assert_refused_before_any_write(world, "apm_modules/ is missing")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_without_apm_modules_an_unresolvable_removal_target_refuses(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4889: a removal target that cannot be resolved is unverified, never dropped."""
    world = _local_world(tmp_path, apm_binary_path)
    _without_modules(world)
    target = world.project / "vendor" / "e"
    shutil.rmtree(target)
    os.symlink(target, target, target_is_directory=True)

    before = tree_digest(world.project)
    # A later dry-run step reports the loop itself; the preview still keeps e.
    preview = world.apm("uninstall", "./vendor/e", "--dry-run", expect=None)
    text = preview.stdout + preview.stderr
    assert "Would keep ./vendor/e" in text and "Traceback" not in text, text
    assert tree_digest(world.project) == before
    _assert_refused_before_any_write(world, "apm_modules/ is missing")


def test_without_apm_modules_a_replaced_survivor_refuses(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4889: replace rules can make a remote survivor local; offline they cannot be proven."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    vendor = world.project / "vendor"
    _skill_package(vendor / "e", "e")
    _skill_package(vendor / "d", "d", ("../e",))
    dump_yaml(
        {
            "$schema": _REPLACEMENTS_SCHEMA,
            "name": "proj",
            "version": "1.0.0",
            "targets": ["claude"],
            "dependencies": {"apm": ["./vendor/e", "test/r"]},
            "replace": [
                {"for": {"git": "https://github.com/test/r"}, "with": {"path": "./vendor/d"}}
            ],
        },
        world.project / "apm.yml",
    )
    (world.project / ".claude").mkdir()
    world.apm("install")
    _without_modules(world)

    _assert_refused_before_any_write(world, "apm_modules/ is missing")


def test_a_hook_bearing_reanchor_refuses_and_keeps_its_hooks(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4889: merged hooks rebuild from the materialization the re-key moves; refuse first."""
    world = _local_world(tmp_path, apm_binary_path)
    vendor = world.project / "vendor"
    hook = {
        "hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "true"}]}]
        }
    }
    write(vendor / "e/.apm/hooks/hooks.json", json.dumps(hook))
    _manifest(world, ["./vendor/e"], ["./vendor/d", "./vendor/a"])
    manifest = load_yaml(world.project / "apm.yml")
    manifest["executables"] = {"allow": {"*": {"hooks": True}}}
    dump_yaml(manifest, world.project / "apm.yml")
    world.apm("install", expect=None)
    settings = world.project / ".claude" / "settings.json"
    hooks_before = settings.read_bytes() if settings.exists() else None

    _assert_refused_before_any_write(world, "ships hooks")
    assert (settings.read_bytes() if settings.exists() else None) == hooks_before


_SHARED_SKILL = ".claude/skills/shared/SKILL.md"


def _shared_skill_world(
    tmp_path: Path,
    apm_binary_path: Path,
    *,
    same: bool,
    e_first: bool = True,
    target: str = "claude",
    install: tuple[str, ...] = (),
) -> ReplayWorld:
    """e and z both ship skill ``shared``; z declares ``../e`` (#5048).

    Skills are last-installed-wins, so the re-anchor's new install order can
    change the final writer of ``shared``.
    """
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    vendor = world.project / "vendor"
    _skill_package(vendor / "e", "e")
    _skill_package(vendor / "z", "z", ("../e",))
    for name, body in (("e", "E"), ("z", "E" if same else "Z")):
        write(
            vendor / name / ".apm/skills/shared/SKILL.md",
            f"---\nname: shared\ndescription: Shared skill\n---\n# {body}\n",
        )
    if e_first:
        _manifest(world, ["./vendor/e"], ["./vendor/z"])
    else:
        _manifest(world, ["./vendor/z", "./vendor/e"], [])
    manifest = load_yaml(world.project / "apm.yml")
    manifest["targets"] = [target]
    dump_yaml(manifest, world.project / "apm.yml")
    (world.project / f".{target}").mkdir()
    world.apm("install", *install)
    return world


def _legacy_plugin_world(tmp_path: Path, apm_binary_path: Path) -> ReplayWorld:
    """Local e, dev d -> e, and a direct local legacy plugin p deployed to Cursor (#5049)."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    vendor = world.project / "vendor"
    _skill_package(vendor / "e", "e")
    _skill_package(vendor / "d", "d", ("../e",))
    write(
        vendor / "p" / ".claude-plugin" / "plugin.json",
        json.dumps({"name": "p", "version": "1.0.0"}),
    )
    write(
        vendor / "p" / "skills" / "p-skill" / "SKILL.md",
        "---\nname: p-skill\ndescription: Skill of p\n---\n# p\n",
    )
    dump_yaml(
        {
            "name": "proj",
            "version": "1.0.0",
            "targets": ["cursor"],
            "dependencies": {"apm": ["./vendor/e", "./vendor/p"]},
            "devDependencies": {"apm": ["./vendor/d"]},
        },
        world.project / "apm.yml",
    )
    (world.project / ".cursor").mkdir()
    world.apm("install")
    return world


@pytest.mark.parametrize("e_first", [True, False], ids=["e-installed-first", "z-installed-first"])
def test_a_reanchor_records_the_shared_skill_writer_a_fresh_install_records(
    tmp_path: Path, apm_binary_path: Path, e_first: bool
) -> None:
    """#5048 cell 1: identical shared bytes re-anchor; --no-frozen keeps owner and bytes."""
    world = _shared_skill_world(tmp_path, apm_binary_path, same=True, e_first=e_first)
    deployed = (world.project / _SHARED_SKILL).read_bytes()

    result = world.apm("uninstall", "./vendor/e")

    output = " ".join((result.stdout + result.stderr).split())
    assert "./vendor/e stays installed because" in output, output
    assert (world.project / _SHARED_SKILL).read_bytes() == deployed
    _assert_reanchored_like_a_full_install(world)
    assert (world.project / _SHARED_SKILL).read_bytes() == deployed
    world.audit_clean()


def test_a_reanchor_that_changes_a_shared_skill_writer_refuses(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#5048 cell 2: the next install would write e's different bytes; refuse first."""
    world = _shared_skill_world(tmp_path, apm_binary_path, same=False)

    _assert_refused_before_any_write(world, ".claude/skills/shared", "would change its writer")


def test_a_reanchor_without_a_receipt_for_a_carried_shared_skill_refuses(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#5048 cell 3: ownership moves only with a receipt APM can verify."""
    world = _shared_skill_world(tmp_path, apm_binary_path, same=True)
    (receipt,) = (world.isolated.home / ".apm" / "deployment-receipts").glob("*.json")
    data = json.loads(receipt.read_text(encoding="utf-8"))
    data["records"] = [row for row in data["records"] if row["value"] != _SHARED_SKILL]
    for section in ("bindings", "destinations", "history"):
        for key in [key for key in data.get(section) or {} if key.endswith(_SHARED_SKILL)]:
            del data[section][key]
    receipt.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    _assert_refused_before_any_write(world, "cannot prove it wrote", _SHARED_SKILL)


def test_a_reanchor_ranks_shared_skill_writers_as_install_orders_them(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#5048: install orders by lock key, which here disagrees with repo_url order.

    After the re-anchor ``local:vendor/e`` and ``local:vendor/a/x`` share depth
    2; by key e installs last and writes ``shared``, by repo_url x would.
    """
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    vendor = world.project / "vendor"
    _skill_package(vendor / "e", "e")
    _skill_package(vendor / "a" / "x", "x")
    _skill_package(vendor / "z", "z", ("../e", "../a/x"))
    for root in (vendor / "e", vendor / "a" / "x"):
        write(
            root / ".apm/skills/shared/SKILL.md",
            "---\nname: shared\ndescription: Shared skill\n---\n# E\n",
        )
    _manifest(world, ["./vendor/e"], ["./vendor/z"])
    (world.project / ".claude").mkdir()
    world.apm("install")
    deployed = (world.project / _SHARED_SKILL).read_bytes()

    world.apm("uninstall", "./vendor/e")

    assert (world.project / _SHARED_SKILL).read_bytes() == deployed
    _assert_reanchored_like_a_full_install(world)


def test_a_reanchor_keeps_a_third_provider_that_still_writes_last(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#5048 R2: e, z and y ship ``shared``; y, never re-ranked, still installs last.

    After the re-anchor ``local:vendor/e`` and ``local:vendor/y`` share depth 2
    and y sorts after e, so a fresh install keeps y as the writer: the
    prediction must weigh every provider, not prefer the re-anchored one.
    """
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    vendor = world.project / "vendor"
    _skill_package(vendor / "e", "e")
    _skill_package(vendor / "y", "y")
    _skill_package(vendor / "z", "z", ("../e", "../y"))
    for name in ("e", "y", "z"):
        write(
            vendor / name / ".apm/skills/shared/SKILL.md",
            "---\nname: shared\ndescription: Shared skill\n---\n# E\n",
        )
    _manifest(world, ["./vendor/e"], ["./vendor/z"])
    (world.project / ".claude").mkdir()
    world.apm("install")
    owners = {
        row["value"]: row["active_owner"]
        for row in load_yaml(world.project / "apm.lock.yaml")["deployments"]
    }
    assert owners[_SHARED_SKILL] == "local:vendor/y", owners

    world.apm("uninstall", "./vendor/e")

    owners = {
        row["value"]: row["active_owner"]
        for row in load_yaml(world.project / "apm.lock.yaml")["deployments"]
    }
    assert owners[_SHARED_SKILL] == "local:vendor/y", owners
    _assert_reanchored_like_a_full_install(world)


def test_a_reanchor_refuses_a_shared_skill_in_a_layout_the_render_cannot_reproduce(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#5048 review: a legacy per-client skill layout is not reproduced offline; refuse."""
    world = _shared_skill_world(
        tmp_path, apm_binary_path, same=False, target="cursor", install=("--legacy-skill-paths",)
    )
    assert (world.project / ".cursor/skills/shared/SKILL.md").is_file()

    _assert_refused_before_any_write(world, ".cursor/skills/")


def test_a_reanchor_carries_a_shared_skill_directory_claim_spelled_with_a_slash(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#5048 review: ``.claude/skills/shared/`` is the same claim as without the "/"."""
    world = _shared_skill_world(tmp_path, apm_binary_path, same=True)
    directory = ".claude/skills/shared"
    lock = load_yaml(world.project / "apm.lock.yaml")
    for row in lock["deployments"]:
        if row["value"] == directory:
            row["value"] = f"{directory}/"
    for row in lock["dependencies"]:
        files = row.get("deployed_files") or []
        row["deployed_files"] = [f"{path}/" if path == directory else path for path in files]
    dump_yaml(lock, world.project / "apm.lock.yaml")

    result = world.apm("uninstall", "./vendor/e")

    output = " ".join((result.stdout + result.stderr).split())
    assert "./vendor/e stays installed because" in output, output
    owners = {
        row["value"].rstrip("/"): row["active_owner"]
        for row in load_yaml(world.project / "apm.lock.yaml")["deployments"]
    }
    assert owners[directory] == owners[_SHARED_SKILL] == "local:vendor/e", owners


def test_a_reanchor_refuses_a_directory_only_shared_skill_claim(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#5048 review: a directory claim alone cannot prove its files' bytes; refuse."""
    world = _shared_skill_world(tmp_path, apm_binary_path, same=False)
    lock = load_yaml(world.project / "apm.lock.yaml")
    lock["deployments"] = [row for row in lock["deployments"] if row["value"] != _SHARED_SKILL]
    for row in lock["dependencies"]:
        if _SHARED_SKILL in (row.get("deployed_files") or []):
            row["deployed_files"].remove(_SHARED_SKILL)
            (row.get("deployed_file_hashes") or {}).pop(_SHARED_SKILL, None)
    dump_yaml(lock, world.project / "apm.lock.yaml")

    _assert_refused_before_any_write(world, ".claude/skills/shared")


def test_a_reanchor_refuses_an_unclaimed_shared_member_whose_bytes_would_change(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#5048 review: under a directory-only claim, a member e' would rewrite refuses.

    y still writes ``shared`` last, but skill members merge: z's ``asset.txt``
    stays, and after the re-anchor e' writes its own over it.
    """
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    vendor = world.project / "vendor"
    _skill_package(vendor / "e", "e")
    _skill_package(vendor / "y", "y")
    _skill_package(vendor / "z", "z", ("../e", "../y"))
    for name in ("e", "y", "z"):
        write(
            vendor / name / ".apm/skills/shared/SKILL.md",
            "---\nname: shared\ndescription: Shared skill\n---\n# S\n",
        )
    write(vendor / "e" / ".apm/skills/shared/asset.txt", "E\n")
    write(vendor / "z" / ".apm/skills/shared/asset.txt", "Z\n")
    _manifest(world, ["./vendor/e"], ["./vendor/z"])
    (world.project / ".claude").mkdir()
    world.apm("install")
    members = ".claude/skills/shared/"
    lock = load_yaml(world.project / "apm.lock.yaml")
    lock["deployments"] = [
        row for row in lock["deployments"] if not row["value"].startswith(members)
    ]
    for row in lock["dependencies"]:
        row["deployed_files"] = [
            path for path in row.get("deployed_files") or [] if not path.startswith(members)
        ]
        hashes = row.get("deployed_file_hashes") or {}
        for path in [path for path in hashes if path.startswith(members)]:
            del hashes[path]
    dump_yaml(lock, world.project / "apm.lock.yaml")
    assert (world.project / members / "asset.txt").read_text(encoding="utf-8") == "Z\n"

    _assert_refused_before_any_write(
        world, ".claude/skills/shared/asset.txt", "has no claim that proves its writer"
    )


def test_a_reanchor_refuses_a_shared_skill_on_a_root_several_targets_deploy(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#5048 review: on .agents/skills the new writer's target is not provable; refuse."""
    world = _shared_skill_world(tmp_path, apm_binary_path, same=True, target="copilot")
    assert (world.project / ".agents/skills/shared/SKILL.md").is_file()

    _assert_refused_before_any_write(world, ".agents/skills/shared", "would change its writer")


def test_a_reanchor_refuses_a_shared_file_whose_owner_declares_no_rule(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#5048 R3: only a rule the primitive's owner declares is trusted; an agent refuses."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    vendor = world.project / "vendor"
    _skill_package(vendor / "e", "e")
    _skill_package(vendor / "z", "z", ("../e",))
    for name in ("e", "z"):
        write(vendor / name / ".apm/agents/shared.agent.md", "---\ndescription: a\n---\n# A\n")
    _manifest(world, ["./vendor/e"], ["./vendor/z"])
    (world.project / ".claude").mkdir()
    world.apm("install")

    _assert_refused_before_any_write(world, ".claude/agents/shared.md", "would change its writer")


def test_an_unchanged_legacy_plugin_survivor_does_not_block_a_reanchor(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#5049: a legacy plugin's copy is normalized at install; its source still matches."""
    world = _legacy_plugin_world(tmp_path, apm_binary_path)
    copy = world.project / "apm_modules" / "_local" / "p"
    assert (copy / "apm.yml").is_file() and not (world.project / "vendor/p/apm.yml").exists()

    result = world.apm("uninstall", "./vendor/e")
    output = " ".join((result.stdout + result.stderr).split())
    assert "./vendor/e stays installed because" in output, output
    assert "Refreshed local package" not in output, output
    rows = _rows(world)
    assert rows["_local/e"]["declaring_parent"] and "_local/p" in rows, sorted(rows)
    _assert_reanchored_like_a_full_install(world)


def test_a_changed_legacy_plugin_survivor_still_refuses_a_reanchor(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#5049: a legacy plugin whose source changed after install still refuses first."""
    world = _legacy_plugin_world(tmp_path, apm_binary_path)
    write(
        world.project / "vendor" / "p" / "skills" / "p-skill" / "SKILL.md",
        "---\nname: p-skill\ndescription: Skill of p\n---\n# p, edited\n",
    )

    _assert_refused_before_any_write(world, "changed since it was installed")
