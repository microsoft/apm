"""Direct regression tests for resolution-staging path relocations."""

from pathlib import Path

import pytest

from apm_cli.install.resolution_staging import ResolutionStagingSession


def test_replacement_keeps_installed_hook_live_until_publish(tmp_path: Path) -> None:
    """A replacement download must not unlink the currently registered hook."""
    modules = tmp_path / "apm_modules"
    package = modules / "owner" / "plugin"
    hook = package / "hooks" / "pre_tool.py"
    hook.parent.mkdir(parents=True)
    hook.write_text("old hook", encoding="ascii")
    staging = ResolutionStagingSession(modules)

    replacement = staging.prepare_replacement(package)

    assert hook.read_text(encoding="ascii") == "old hook"
    replacement_hook = replacement / "hooks" / "pre_tool.py"
    replacement_hook.parent.mkdir(parents=True)
    replacement_hook.write_text("new hook", encoding="ascii")

    staging.publish_replacement(replacement)

    assert hook.read_text(encoding="ascii") == "new hook"
    staging.rollback()
    assert hook.read_text(encoding="ascii") == "old hook"


@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt])
def test_publish_replacement_restores_old_package_when_activation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[BaseException],
) -> None:
    """A failed activation must leave the previous hook runnable."""
    modules = tmp_path / "apm_modules"
    package = modules / "owner" / "plugin"
    hook = package / "hooks" / "pre_tool.py"
    hook.parent.mkdir(parents=True)
    hook.write_text("old hook", encoding="ascii")
    staging = ResolutionStagingSession(modules)
    replacement = staging.prepare_replacement(package)
    replacement.mkdir(parents=True)
    real_replace = Path.replace

    def fail_replacement(source: Path, target: Path) -> Path:
        if source == replacement:
            raise error_type("injected activation failure")
        return real_replace(source, target)

    monkeypatch.setattr(Path, "replace", fail_replacement)

    with pytest.raises(error_type, match="injected activation failure"):
        staging.publish_replacement(replacement)

    assert hook.read_text(encoding="ascii") == "old hook"
    staging.rollback()
    assert not (modules / ".apm-resolution-staging").exists()


def test_prepare_replacement_reserves_destination(tmp_path: Path) -> None:
    """Parallel callbacks cannot materialize into one physical package path."""
    modules = tmp_path / "apm_modules"
    package = modules / "owner" / "plugin"
    staging = ResolutionStagingSession(modules)

    replacement = staging.prepare_replacement(package)

    with pytest.raises(RuntimeError, match="replacement in progress"):
        staging.prepare_replacement(package)

    staging.discard_replacement(replacement)
    assert staging.prepare_replacement(package) == replacement


@pytest.mark.parametrize("publish_parent_first", [False, True])
def test_nested_replacements_rollback_without_overlapping_staging_paths(
    tmp_path: Path,
    publish_parent_first: bool,
) -> None:
    """Parent and virtual-subdirectory replacements restore exact prior bytes."""
    modules = tmp_path / "apm_modules"
    parent = modules / "owner" / "repo"
    child = parent / "plugins" / "tool"
    child.mkdir(parents=True)
    (parent / "apm.yml").write_text("old manifest", encoding="ascii")
    (parent / "keep.txt").write_text("keep", encoding="ascii")
    (child / "hook.py").write_text("old child", encoding="ascii")
    staging = ResolutionStagingSession(modules)
    parent_replacement = staging.prepare_replacement(parent)
    child_replacement = staging.prepare_replacement(child)
    (parent_replacement / "plugins" / "tool").mkdir(parents=True)
    (parent_replacement / "apm.yml").write_text("new manifest", encoding="ascii")
    (parent_replacement / "plugins" / "tool" / "hook.py").write_text(
        "parent child",
        encoding="ascii",
    )
    child_replacement.mkdir(parents=True)
    (child_replacement / "hook.py").write_text("new child", encoding="ascii")

    replacements = (
        (parent_replacement, child_replacement)
        if publish_parent_first
        else (child_replacement, parent_replacement)
    )
    for replacement in replacements:
        staging.publish_replacement(replacement)
    staging.rollback()

    assert (parent / "apm.yml").read_text(encoding="ascii") == "old manifest"
    assert (parent / "keep.txt").read_text(encoding="ascii") == "keep"
    assert (child / "hook.py").read_text(encoding="ascii") == "old child"


def test_replacement_reservation_uses_canonical_staging_path(tmp_path: Path) -> None:
    """A symlinked modules root uses the same reservation key at publication."""
    actual_modules = tmp_path / "actual-modules"
    actual_modules.mkdir()
    modules = tmp_path / "apm_modules"
    modules.symlink_to(actual_modules, target_is_directory=True)
    package = modules / "owner" / "plugin"
    staging = ResolutionStagingSession(modules)
    replacement = staging.prepare_replacement(package)
    replacement.mkdir(parents=True)
    (replacement / "apm.yml").write_text("new", encoding="ascii")

    live_path = staging.publish_replacement(replacement)

    assert live_path == package.resolve()
    assert (package / "apm.yml").read_text(encoding="ascii") == "new"


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
