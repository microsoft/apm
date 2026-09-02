"""Contracts for the deterministic, fail-closed repository inventory."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.architecture_linter.inventory import (
    EXCLUDED_ROOTS,
    REQUIRED_FILES,
    REQUIRED_ROOTS,
    Inventory,
    InventoryError,
    build_inventory,
)


def _write_minimal_repo(
    root: Path,
    *,
    omit_roots: tuple[str, ...] = (),
    omit_files: tuple[str, ...] = (),
) -> None:
    """Write the smallest tree that satisfies every required anchor.

    `omit_roots`/`omit_files` skip specific anchors so a test can exercise
    exactly one missing-anchor failure at a time. A required file nested
    under an omitted root (e.g. ``.apm/architecture/owners/index.json``
    under ``.apm``) is skipped too -- writing it would silently recreate
    the very root the test wants left absent.
    """
    omitted_files = set(omit_files) | {
        name
        for name in REQUIRED_FILES
        if any(name == root_name or name.startswith(f"{root_name}/") for root_name in omit_roots)
    }
    for name in REQUIRED_ROOTS:
        if name not in omit_roots:
            (root / name).mkdir(parents=True, exist_ok=True)
    for name in REQUIRED_FILES:
        if name in omitted_files:
            continue
        file_path = root / name
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("", encoding="utf-8")


def test_excluded_roots_constant_is_exact_and_stable() -> None:
    """The excluded-roots allow-list is a fixed, fully enumerated tuple."""
    assert EXCLUDED_ROOTS == (
        ".git",
        ".venv",
        "venv",
        "env",
        "ENV",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "build",
        "dist",
        "htmlcov",
        "mutants",
        ".idea",
        ".vscode",
    )
    assert len(set(EXCLUDED_ROOTS)) == len(EXCLUDED_ROOTS)


def test_required_roots_and_files_constants_are_exact() -> None:
    """The fail-closed anchors are a fixed, fully enumerated tuple each."""
    assert REQUIRED_ROOTS == ("src", "scripts", "tests", ".apm")
    assert REQUIRED_FILES == ("pyproject.toml", ".apm/architecture/owners/index.json")


def test_build_inventory_is_deterministic_across_repeated_calls(tmp_path: Path) -> None:
    """The same tree always yields the same sorted tuple of files."""
    _write_minimal_repo(tmp_path)
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "z_module.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "src" / "pkg" / "a_module.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "scripts" / "tool.py").write_text("x = 1\n", encoding="utf-8")

    first = build_inventory(tmp_path)
    second = build_inventory(tmp_path)

    assert isinstance(first, Inventory)
    assert first == second
    assert first.files == tuple(sorted(first.files))
    assert first.root == tmp_path.resolve().as_posix()
    assert first.excluded_roots == EXCLUDED_ROOTS
    assert "src/pkg/a_module.py" in first.files
    assert "src/pkg/z_module.py" in first.files
    assert "scripts/tool.py" in first.files


def test_build_inventory_excludes_configured_roots_even_when_nested(tmp_path: Path) -> None:
    """Unambiguously generated directory names are pruned at every depth."""
    _write_minimal_repo(tmp_path)
    (tmp_path / ".git" / "objects").mkdir(parents=True)
    (tmp_path / ".git" / "objects" / "pack").write_text("junk", encoding="utf-8")
    (tmp_path / "node_modules" / "left-pad").mkdir(parents=True)
    (tmp_path / "node_modules" / "left-pad" / "index.js").write_text("", encoding="utf-8")
    (tmp_path / "src" / "pkg" / "__pycache__").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__pycache__" / "mod.cpython-312.pyc").write_bytes(b"\x00")
    (tmp_path / "tests" / ".pytest_cache").mkdir(parents=True)
    (tmp_path / "tests" / ".pytest_cache" / "README.md").write_text("", encoding="utf-8")
    (tmp_path / "src" / "pkg" / "kept.py").write_text("x = 1\n", encoding="utf-8")

    inventory = build_inventory(tmp_path)

    assert "src/pkg/kept.py" in inventory.files
    assert not any(".git" in path.split("/") for path in inventory.files)
    assert not any("node_modules" in path.split("/") for path in inventory.files)
    assert not any("__pycache__" in path.split("/") for path in inventory.files)
    assert not any(".pytest_cache" in path.split("/") for path in inventory.files)


def test_root_only_exclusions_do_not_hide_tracked_source_names(tmp_path: Path) -> None:
    """Names such as env/build are allowed below source and test roots."""
    _write_minimal_repo(tmp_path)
    nested_env = tmp_path / "tests" / "red_team" / "env"
    nested_build = tmp_path / "src" / "fixtures" / "build"
    nested_env.mkdir(parents=True)
    nested_build.mkdir(parents=True)
    (nested_env / "test_command_env.py").write_text("x = 1\n", encoding="utf-8")
    (nested_build / "fixture.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "env").mkdir()
    (tmp_path / "env" / "ignored.py").write_text("x = 1\n", encoding="utf-8")

    inventory = build_inventory(tmp_path)

    assert "tests/red_team/env/test_command_env.py" in inventory.files
    assert "src/fixtures/build/fixture.py" in inventory.files
    assert "env/ignored.py" not in inventory.files


def test_gitdir_file_is_excluded_from_worktree_inventory(tmp_path: Path) -> None:
    """A worktree's root .git file is excluded like a normal .git directory."""
    _write_minimal_repo(tmp_path)
    (tmp_path / ".git").write_text("gitdir: ../.git/worktrees/demo\n", encoding="utf-8")

    inventory = build_inventory(tmp_path)

    assert ".git" not in inventory.files


def test_real_nested_env_test_modules_remain_in_inventory() -> None:
    """Tracked red-team tests below a directory named env stay enforceable."""
    root = Path(__file__).resolve().parents[3]
    expected = {
        path.relative_to(root).as_posix() for path in (root / "tests/red_team/env").rglob("*.py")
    }

    inventory = build_inventory(root)

    assert expected
    assert expected <= set(inventory.files)


def test_build_inventory_excludes_egg_info_suffixed_directories(tmp_path: Path) -> None:
    """Any directory ending in ``.egg-info`` is generated content, never source."""
    _write_minimal_repo(tmp_path)
    (tmp_path / "apm_cli.egg-info").mkdir()
    (tmp_path / "apm_cli.egg-info" / "PKG-INFO").write_text("", encoding="utf-8")
    (tmp_path / "src" / "kept.py").write_text("x = 1\n", encoding="utf-8")

    inventory = build_inventory(tmp_path)

    assert "src/kept.py" in inventory.files
    assert not any(path.startswith("apm_cli.egg-info/") for path in inventory.files)


def test_build_inventory_paths_are_repository_relative_posix(tmp_path: Path) -> None:
    """Every listed path is repository-relative and forward-slash separated."""
    _write_minimal_repo(tmp_path)
    nested = tmp_path / "src" / "pkg" / "sub"
    nested.mkdir(parents=True)
    (nested / "leaf.py").write_text("x = 1\n", encoding="utf-8")

    inventory = build_inventory(tmp_path)

    assert "src/pkg/sub/leaf.py" in inventory.files
    assert all("\\" not in path for path in inventory.files)
    assert all(not path.startswith("/") for path in inventory.files)


@pytest.mark.parametrize("missing_root", ["src", "scripts", "tests", ".apm"])
def test_missing_required_root_fails_closed(tmp_path: Path, missing_root: str) -> None:
    """Any missing required root directory refuses to lint a partial tree."""
    _write_minimal_repo(tmp_path, omit_roots=(missing_root,))

    with pytest.raises(InventoryError, match=r"required root\(s\) missing") as exc_info:
        build_inventory(tmp_path)
    assert missing_root in str(exc_info.value)


@pytest.mark.parametrize("missing_file", ["pyproject.toml", ".apm/architecture/owners/index.json"])
def test_missing_required_file_fails_closed(tmp_path: Path, missing_file: str) -> None:
    """Any missing required file refuses to lint a partial tree."""
    _write_minimal_repo(tmp_path, omit_files=(missing_file,))

    with pytest.raises(InventoryError, match=r"required file\(s\) missing") as exc_info:
        build_inventory(tmp_path)
    assert missing_file in str(exc_info.value)


def test_multiple_missing_roots_are_reported_together_and_sorted(tmp_path: Path) -> None:
    """A multi-anchor failure names every missing root in one sorted message."""
    _write_minimal_repo(tmp_path, omit_roots=("tests", ".apm"))

    with pytest.raises(InventoryError, match=r"required root\(s\) missing") as exc_info:
        build_inventory(tmp_path)

    message = str(exc_info.value)
    assert message.index(".apm") < message.index("tests")


def test_missing_roots_are_checked_before_missing_files(tmp_path: Path) -> None:
    """A tree missing both a root and a file fails on the root check first."""
    _write_minimal_repo(tmp_path, omit_roots=("src",), omit_files=("pyproject.toml",))

    with pytest.raises(InventoryError, match=r"required root\(s\) missing"):
        build_inventory(tmp_path)


def test_root_must_be_an_existing_directory(tmp_path: Path) -> None:
    """A root that resolves to a file (not a directory) fails closed."""
    not_a_directory = tmp_path / "not_a_directory"
    not_a_directory.write_text("", encoding="utf-8")

    with pytest.raises(InventoryError, match="root is not a directory"):
        build_inventory(not_a_directory)


def test_root_must_exist(tmp_path: Path) -> None:
    """A root that does not exist at all fails closed the same way."""
    missing = tmp_path / "does-not-exist"

    with pytest.raises(InventoryError, match="root is not a directory"):
        build_inventory(missing)
