"""Direct regression tests for resolution-staging path relocations."""

import os
import re
from pathlib import Path

import pytest

from apm_cli.install.resolution_staging import ResolutionStagingSession

pytestmark = pytest.mark.component


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


def test_owns_path_reports_reserved_and_published_slots(tmp_path: Path) -> None:
    """A slot this session is replacing, or already replaced, is owned by it (#3401)."""
    modules = tmp_path / "apm_modules"
    package = modules / "_local" / "shared"
    package.mkdir(parents=True)
    staging = ResolutionStagingSession(modules)

    assert staging.owns_path(package) is False
    replacement = staging.prepare_replacement(package)
    assert staging.owns_path(package) is True
    replacement.mkdir(parents=True, exist_ok=True)
    staging.publish_replacement(replacement)
    assert staging.owns_path(package) is True
    assert staging.owns_path(modules / "_local" / "other") is False


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

    with pytest.raises(
        FileNotFoundError, match="Run 'apm install' to materialize the package again"
    ):
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
def test_cross_parent_relocation_prunes_emptied_source_ancestors(tmp_path: Path) -> None:
    """Moving the last package out of an owner tree must not leave it behind.

    On case-sensitive filesystems ``Test/C/packages/a`` moves beside
    ``test/c/packages/b``; a leftover empty ``Test/C`` tree is later counted
    as a second package directory for ``test/c``. Rollback restores the tree.
    """
    modules = tmp_path / "apm_modules"
    source = modules / "Old" / "c" / "packages" / "a"
    source.mkdir(parents=True)
    (source / "apm.yml").write_text("name: a\n", encoding="utf-8")
    sibling = modules / "new" / "c" / "packages" / "b"
    sibling.mkdir(parents=True)
    staging = ResolutionStagingSession(modules)

    destination = modules / "new" / "c" / "packages" / "a"
    staging.relocate_path(source, destination)

    assert (destination / "apm.yml").is_file()
    assert not (modules / "Old").exists()
    assert sibling.is_dir()

    staging.rollback()

    assert (source / "apm.yml").read_text(encoding="utf-8") == "name: a\n"
    assert not destination.exists()
    assert sibling.is_dir()


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


@pytest.mark.windows_compat
def test_prepare_replacement_slot_names_fit_windows_max_path(tmp_path: Path) -> None:
    """A realistic staged path must not overflow Windows MAX_PATH (issue #2896).

    Before the fix, every staged path carried a 32-hex-char staging root
    (``uuid4().hex``) plus a 64-hex-char per-destination slot
    (``sha256(...).hexdigest()``) -- 96 hex characters of pure entropy on
    top of the project root and a package's own nested directories. Added to
    a realistic Windows project location that reliably pushed staged paths
    past the 260-character MAX_PATH, raising
    ``[WinError 206] The filename or extension is too long.``. This guards
    the fix: the staging root is now 12 hex chars and the per-destination
    slot is 16 (28 total), freeing 68 characters on every staged path.
    """
    modules = tmp_path / "apm_modules"
    destination = (
        modules
        / "acme-platform-org"
        / "enterprise-notification-templates-package"
        / "src"
        / "templates"
        / "email"
        / "transactional"
    )
    staging = ResolutionStagingSession(modules)

    replacement = staging.prepare_replacement(destination)

    staging_root_name = replacement.parents[1].name
    slot_name = replacement.name
    assert re.fullmatch(r"[0-9a-f]{12}", staging_root_name), staging_root_name
    assert re.fullmatch(r"[0-9a-f]{16}", slot_name), slot_name

    # Re-root the real, generated relative path (unmocked, straight out of
    # prepare_replacement) under a realistic Windows project location -- a
    # OneDrive-synced repo checkout, a common enterprise layout -- to check
    # the MAX_PATH arithmetic the issue describes independent of this test
    # run's own (highly variable) tmp_path length.
    realistic_root = (
        r"C:\Users\jennifer.smith\OneDrive - Contoso Corporation\Documents"
        r"\GitHub\internal-tools-platform\services\billing-reconciliation-worker"
    )
    relative_len = len(str(replacement.relative_to(modules))) + len("apm_modules") + 1
    old_scheme_relative_len = relative_len + (32 - 12) + (64 - 16)

    assert len(realistic_root) + 1 + relative_len <= 260
    assert len(realistic_root) + 1 + old_scheme_relative_len > 260

    # The assertions above stop at the slot directory, but real installs write the
    # package's own files beneath it, and that deepest staged file is the budget
    # that actually matters. Express it as headroom -- the longest project root
    # that still fits -- so the guard does not depend on one hand-picked path.
    nested_payload = Path("partials") / "order-confirmation-email-template.html.hbs"
    nested_len = relative_len + 1 + len(str(nested_payload))
    old_scheme_nested_len = old_scheme_relative_len + 1 + len(str(nested_payload))
    root_budget = 260 - 1 - nested_len
    old_scheme_root_budget = 260 - 1 - old_scheme_nested_len

    assert root_budget - old_scheme_root_budget == (32 - 12) + (64 - 16)

    # A project root between those two budgets installs only after the fix. Note
    # this is headroom, not a long-path guarantee: the 134-character OneDrive root
    # above still overflows once nested package content is appended.
    moderate_root = (
        r"C:\Users\jennifer.smith\source\repos\internal-tools-platform"
        r"\services\billing-reconciliation-worker"
    )
    assert old_scheme_root_budget < len(moderate_root) <= root_budget
    assert len(moderate_root) + 1 + nested_len <= 260
    assert len(moderate_root) + 1 + old_scheme_nested_len > 260


@pytest.mark.parametrize("child_first", [True, False])
def test_nested_copies_stage_in_either_order_and_roll_back(
    tmp_path: Path, child_first: bool
) -> None:
    modules = tmp_path / "apm_modules"
    parent = modules / "owner" / "repo"
    child = parent / "plugins" / "tool"
    child.mkdir(parents=True)
    (parent / "apm.yml").write_text("name: repo\n", encoding="utf-8")
    (child / "apm.yml").write_text("name: tool\n", encoding="utf-8")
    staging = ResolutionStagingSession(modules)

    for path in (child, parent) if child_first else (parent, child):
        staging.prepare_path(path)
    assert not parent.exists()
    child.mkdir(parents=True)
    (child / "apm.yml").write_text("fetched\n", encoding="utf-8")

    assert staging.rollback() == []
    assert (parent / "apm.yml").read_text(encoding="utf-8") == "name: repo\n"
    assert (child / "apm.yml").read_text(encoding="utf-8") == "name: tool\n"


def test_a_no_clobber_publish_never_replaces_an_existing_destination(tmp_path: Path) -> None:
    """``clobber=False`` fails on anything at the destination and leaves it as it was."""
    modules = tmp_path / "apm_modules"
    destination = modules / "o" / "p"
    destination.parent.mkdir(parents=True)
    destination.write_text("user\n", encoding="utf-8")
    session = ResolutionStagingSession(modules)
    replacement = session.prepare_replacement(destination)
    replacement.mkdir(parents=True)
    (replacement / "apm.yml").write_text("name: p\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        session.publish_replacement(replacement, clobber=False)

    assert destination.read_text(encoding="utf-8") == "user\n"
    assert session.rollback() == []
    assert destination.read_text(encoding="utf-8") == "user\n"


def test_a_no_clobber_publish_never_replaces_an_empty_directory(tmp_path: Path) -> None:
    """A plain POSIX rename replaces an empty directory; the no-replace publish must not."""
    modules = tmp_path / "apm_modules"
    destination = modules / "o" / "p"
    session = ResolutionStagingSession(modules)
    replacement = session.prepare_replacement(destination)
    replacement.mkdir(parents=True)
    (replacement / "apm.yml").write_text("name: p\n", encoding="utf-8")
    destination.mkdir(parents=True)  # appeared after planning, e.g. another writer

    with pytest.raises(OSError):
        session.publish_replacement(replacement, clobber=False)

    assert destination.is_dir() and not any(destination.iterdir())


def test_rollback_keeps_a_directory_that_replaced_a_published_package(tmp_path: Path) -> None:
    """Rollback removes a published package only while it is still that directory."""
    modules = tmp_path / "apm_modules"
    destination = modules / "o" / "p"
    session = ResolutionStagingSession(modules)
    replacement = session.prepare_replacement(destination)
    replacement.mkdir(parents=True)
    (replacement / "apm.yml").write_text("name: p\n", encoding="utf-8")
    session.publish_replacement(replacement, clobber=False)
    # Move ours aside (removing it could let the new directory reuse its inode).
    destination.rename(tmp_path / "ours")
    destination.mkdir()
    (destination / "user.md").write_text("user\n", encoding="utf-8")

    issues = session.rollback()

    assert [path for path, _reason in issues] == [destination]
    assert (destination / "user.md").read_text(encoding="utf-8") == "user\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory identity")
def test_a_destination_substituted_right_after_publish_is_never_taken_for_ours(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The publish records the staged copy's identity, not whatever sits there after."""
    modules = tmp_path / "apm_modules"
    destination = modules / "o" / "p"
    session = ResolutionStagingSession(modules)
    replacement = session.prepare_replacement(destination)
    replacement.mkdir(parents=True)
    (replacement / "apm.yml").write_text("name: p\n", encoding="utf-8")
    real_lstat = os.lstat
    swapped: list[bool] = []

    def racing_lstat(path, *args, **kwargs):
        if Path(path) == destination and not swapped:
            # Another writer replaces the published directory before it is inspected.
            swapped.append(True)
            destination.rename(tmp_path / "ours")
            destination.mkdir()
            (destination / "user.md").write_text("user\n", encoding="utf-8")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr("os.lstat", racing_lstat)
    session.publish_replacement(replacement, clobber=False)
    issues = session.rollback()
    monkeypatch.undo()

    assert (destination / "user.md").read_text(encoding="utf-8") == "user\n"
    assert [path for path, _reason in issues] == [destination]


def test_removing_a_created_apm_modules_after_commit_leaves_none_behind(tmp_path: Path) -> None:
    """Commit keeps the directory; the locked apply removes it when empty (#5159 I2)."""
    modules = tmp_path / "apm_modules"
    session = ResolutionStagingSession(modules)
    session.prepare_replacement(modules / "scratch").mkdir()

    assert session.commit() == []
    assert modules.is_dir(), "the full run keeps the apm_modules it created"
    assert session.remove_created_modules_dir() == []
    assert not modules.exists()


def test_removing_created_apm_modules_keeps_one_that_existed_or_holds_a_package(
    tmp_path: Path,
) -> None:
    """Only an empty apm_modules this session created is removed."""
    existed = tmp_path / "existed" / "apm_modules"
    existed.mkdir(parents=True)
    session = ResolutionStagingSession(existed)
    session.prepare_replacement(existed / "scratch").mkdir()
    assert session.commit() == []
    assert session.remove_created_modules_dir() == []
    assert existed.is_dir()

    modules = tmp_path / "fresh" / "apm_modules"
    destination = modules / "o" / "p"
    session = ResolutionStagingSession(modules)
    replacement = session.prepare_replacement(destination)
    replacement.mkdir(parents=True)
    (replacement / "apm.yml").write_text("name: p\n", encoding="utf-8")
    session.publish_replacement(replacement, clobber=False)
    assert session.commit() == []
    assert session.remove_created_modules_dir() == []
    assert (destination / "apm.yml").is_file()
