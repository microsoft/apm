"""Direct regression tests for resolution-staging path relocations."""

from pathlib import Path

import pytest

from apm_cli.install.resolution_staging import ResolutionStagingSession


def test_relocate_path_rejects_symlinked_package_directory(tmp_path: Path) -> None:
    """Migration never follows a package-directory symlink."""
    modules = tmp_path / "apm_modules"
    real_package = modules / "real"
    real_package.mkdir(parents=True)
    linked_package = modules / "linked"
    linked_package.symlink_to(real_package, target_is_directory=True)
    staging = ResolutionStagingSession(modules)

    with pytest.raises(ValueError, match="symlinked package directory"):
        staging.relocate_path(linked_package, modules / "Linked")

    assert linked_package.is_symlink()
    assert real_package.is_dir()


def test_relocate_path_reports_disappeared_source(tmp_path: Path) -> None:
    """A concurrent source removal produces an actionable retry error."""
    modules = tmp_path / "apm_modules"
    modules.mkdir()
    staging = ResolutionStagingSession(modules)

    with pytest.raises(FileNotFoundError, match="Run 'apm install' again"):
        staging.relocate_path(modules / "missing", modules / "Missing")


def test_relocation_rollback_reverses_nested_case_changes(tmp_path: Path) -> None:
    """Nested owner/repository renames roll back in reverse order."""
    modules = tmp_path / "apm_modules"
    original_owner = modules / "mixedorg"
    original_repo = original_owner / "mixedrepo"
    original_repo.mkdir(parents=True)
    (original_repo / "marker.txt").write_text("owned", encoding="utf-8")
    staging = ResolutionStagingSession(modules)

    display_owner = modules / "MixedOrg"
    staging.relocate_path(original_owner, display_owner)
    display_repo = display_owner / "MixedRepo"
    staging.relocate_path(display_owner / "mixedrepo", display_repo)

    assert (display_repo / "marker.txt").read_text(encoding="utf-8") == "owned"

    staging.rollback()

    assert (original_repo / "marker.txt").read_text(encoding="utf-8") == "owned"
    assert [path.name for path in modules.iterdir()] == ["mixedorg"]


@pytest.mark.windows_compat
def test_case_only_relocation_updates_spelling_and_rolls_back(tmp_path: Path) -> None:
    """Case-only rename behavior is identical on sensitive and insensitive filesystems."""
    modules = tmp_path / "apm_modules"
    source = modules / "mixedorg"
    source.mkdir(parents=True)
    staging = ResolutionStagingSession(modules)

    destination = modules / "MixedOrg"
    staging.relocate_path(source, destination)
    assert [path.name for path in modules.iterdir()] == ["MixedOrg"]

    staging.rollback()
    assert [path.name for path in modules.iterdir()] == ["mixedorg"]
