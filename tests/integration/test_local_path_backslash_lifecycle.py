"""Backslash local paths install like their ``/`` forms on POSIX (#4699).

PROMISE: req-mf-016 -- ``.\\``, ``..\\`` and ``~\\`` dependency strings are
local-path entries, normalised to POSIX form on read (OpenAPM v0.1 Section
4.3.1), so they resolve exactly like ``./``, ``../`` and ``~/``
(source: docs/src/content/docs/specs/openapm-v0.1.md).
CELL: OP=install SRC=local-path PRIM=apm-package SCOPE=project LOCK=absent
TGT=copilot SPELLING=backslash|slash

Windows absolute forms (``C:\\x``, ``\\\\server\\share``) are not normalised and
stay refused on POSIX without writing the project.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._install_audit_replay_kit import ReplayWorld, apm_package, tree_digest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.skipif(sys.platform == "win32", reason="backslash is a separator on Windows"),
    pytest.mark.promise("req-mf-016", "P-INV-I1", "P-INV-I2"),
]


def _source(world: ReplayWorld, anchor: str, name: str) -> Path:
    """Author package *name* where the ``anchor`` prefix resolves from the project."""
    base = {
        ".": world.project,
        "..": world.project.parent,
        "../..": world.project.parent.parent,
        "~": world.isolated.home,
    }[anchor]
    root = base / name
    apm_package(root, name)
    return root


def _manifest(world: ReplayWorld, entry: str) -> None:
    dump_yaml(
        {
            "name": "proj",
            "version": "1.0.0",
            "targets": ["copilot"],
            "dependencies": {"apm": [entry]},
        },
        world.project / "apm.yml",
    )


def _deployed_state(world: ReplayWorld) -> tuple[dict[str, bytes], list, list]:
    """The deployed tree (without the manifest) and the lock rows a user can see."""
    tree = {
        path: data
        for path, data in tree_digest(world.project).items()
        # .apm-local/installation is a per-checkout random id, not deployed content.
        if not path.startswith(".apm-local/")
    }
    tree.pop("apm.yml")
    tree.pop("apm.lock.yaml")
    lock = load_yaml(world.project / "apm.lock.yaml")
    return tree, lock["dependencies"], lock["deployments"]


@pytest.mark.parametrize(
    ("anchor", "name", "entry"),
    [
        pytest.param(".", "vendored", ".\\vendored", id="dot"),
        pytest.param("..", "shared-skills", "..\\shared-skills", id="dotdot"),
        pytest.param("~", "homepkg", "~\\homepkg", id="home"),
        # Parity, not containment: a root-declared path may leave the project
        # root (#4660), so the escaping backslash form behaves like ../../x.
        pytest.param("../..", "outer", "..\\..\\outer", id="escaping-root-parity"),
        pytest.param("../..", "outer", "..\\../outer", id="mixed-root-parity"),
    ],
)
def test_backslash_local_path_installs_like_its_slash_form(
    tmp_path: Path, apm_binary_path: Path, anchor: str, name: str, entry: str
) -> None:
    slash = ReplayWorld.create(tmp_path / "slash", apm_binary_path)
    _source(slash, anchor, name)
    _manifest(slash, f"{anchor}/{name}")
    slash.apm("install")
    expected_tree, expected_rows, expected_deployments = _deployed_state(slash)
    assert any(path.startswith(".github/instructions/") for path in expected_tree)

    world = ReplayWorld.create(tmp_path / "backslash", apm_binary_path)
    source = _source(world, anchor, name)
    _manifest(world, entry)
    manifest = (world.project / "apm.yml").read_bytes()

    result = world.apm("install", expect=None)

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "traversal" not in result.stdout + result.stderr
    tree, rows, deployments = _deployed_state(world)
    assert tree == expected_tree
    assert rows == expected_rows
    assert rows[0]["local_path"] == f"{anchor}/{name}"
    assert deployments == expected_deployments
    assert (world.project / "apm.yml").read_bytes() == manifest
    materialized = world.project / "apm_modules" / "_local" / name
    assert (materialized / "apm.yml").read_bytes() == (source / "apm.yml").read_bytes()

    # I2: a converged install changes no bytes.
    before = tree_digest(world.project)
    world.apm("install")
    assert tree_digest(world.project) == before


@pytest.mark.parametrize(
    ("entry", "diagnostic"),
    [
        pytest.param("C:\\x", "Local package path does not exist: C:\\x", id="drive-letter"),
        pytest.param("\\\\server\\share", "Invalid APM dependency '\\\\server\\share'", id="unc"),
    ],
)
def test_windows_absolute_local_path_stays_refused_on_posix(
    tmp_path: Path, apm_binary_path: Path, entry: str, diagnostic: str
) -> None:
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    _manifest(world, entry)
    before = tree_digest(world.project)

    result = world.apm("install", expect=1)

    assert diagnostic in " ".join((result.stdout + result.stderr).split())
    assert tree_digest(world.project) == before
    assert not (world.project / "apm_modules").exists()
