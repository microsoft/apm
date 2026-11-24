"""An offline local uninstall stays fail-closed when apm_modules/ is missing, empty or partial.

``apm uninstall ./vendor/e`` must not drop a local row that a surviving package
still needs. Without the installed graph the check runs offline and refuses
before any write (#4889); apm.yml replace rules count (#5031). These cells probe
the neighbours of that guard: the dry-run preview, a cold clone that keeps an
empty apm_modules/ directory, and an interrupted install that left only some
packages in apm_modules/.
Owner: commands/uninstall/local_reanchor.py (_refuse_unverifiable_local_survivors).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._install_audit_replay_kit import ReplayWorld, tree_digest
from tests.integration.test_uninstall_local_reanchor_lifecycle import (
    _REPLACEMENTS_SCHEMA,
    _assert_refused_before_any_write,
    _local_world,
    _manifest,
    _receipts,
    _rows,
    _skill_package,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I3", "P-INV-I7"),
]


def _installed(world: ReplayWorld) -> dict[str, bytes]:
    """apm_modules/ bytes, which tree_digest leaves out."""
    root = world.project / "apm_modules"
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _state(world: ReplayWorld) -> tuple:
    """Everything a refused uninstall must leave alone: project, apm_modules/, HOME receipts."""
    return tree_digest(world.project), _installed(world), _receipts(world)


def _replace_only_in_apm_yml(world: ReplayWorld) -> None:
    manifest = load_yaml(world.project / "apm.yml")
    manifest["$schema"] = _REPLACEMENTS_SCHEMA
    manifest["replace"] = [
        {"for": {"git": "https://github.com/test/r"}, "with": {"path": "./vendor/e"}}
    ]
    dump_yaml(manifest, world.project / "apm.yml")


def test_offline_dry_run_previews_the_keep_an_apm_yml_replace_rule_forces(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#5031 parity: the preview names the kept package and writes nothing."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    vendor = world.project / "vendor"
    _skill_package(vendor / "e", "e")
    _skill_package(vendor / "x", "x")
    _manifest(world, ["./vendor/e", "./vendor/x"], [])
    (world.project / ".claude").mkdir()
    world.apm("install")
    _replace_only_in_apm_yml(world)
    shutil.rmtree(world.project / "apm_modules")
    before = (tree_digest(world.project), _receipts(world))

    preview = world.apm("uninstall", "./vendor/e", "--dry-run")
    text = " ".join((preview.stdout + preview.stderr).split())

    assert "Would keep" in text and "apm_modules/ is missing" in text, text
    assert (tree_digest(world.project), _receipts(world)) == before
    _assert_refused_before_any_write(world, "apm_modules/ is missing")


@pytest.mark.parametrize("modules", ["empty-dir", "survivor-missing", "removed-missing"])
def test_a_still_needed_local_row_survives_an_incomplete_apm_modules(
    modules: str, tmp_path: Path, apm_binary_path: Path
) -> None:
    """d still needs e: removing e's declaration keeps e's row, or refuses with nothing written."""
    world = _local_world(tmp_path, apm_binary_path)
    installed = world.project / "apm_modules"
    if modules == "empty-dir":  # a cold clone that commits apm_modules/.gitkeep
        shutil.rmtree(installed)
        installed.mkdir()
        (installed / ".gitkeep").write_text("")
    else:  # an interrupted install
        shutil.rmtree(
            installed / "_local" / {"survivor-missing": "d", "removed-missing": "e"}[modules]
        )
    before = _state(world)

    result = world.apm("uninstall", "./vendor/e", expect=None)
    text = " ".join((result.stdout + result.stderr).split())

    assert "Traceback" not in text, text
    if modules == "survivor-missing":
        # The lock still records d -> e, so e's declaration goes and its row stays.
        assert result.returncode == 0, text
        assert "./vendor/e stays installed because _local/d still depends on it" in text, text
        assert "./vendor/e" not in str(load_yaml(world.project / "apm.yml")["dependencies"])
        row = _rows(world)["_local/e"]
        assert (row["local_path"], row["declaring_parent"], row["anchored_local_path"]) == (
            "../e",
            "./vendor/d",
            "vendor/e",
        )
        shutil.rmtree(installed)
        world.apm("install", "--frozen")
        return
    assert result.returncode == 1, text
    assert "its apm_modules copy _local/e is missing" in text, text
    assert "apm.yml and apm.lock.yaml were not changed" in text, text
    assert _state(world) == before
