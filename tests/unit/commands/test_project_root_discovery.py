"""Project root discovery from a subdirectory (#4545): ownership and path rebasing."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-install-command-scope-selection")]


# -- project root discovery trusts only the user's own ancestor manifest (#4545) --


@pytest.mark.skipif(not hasattr(__import__("os"), "geteuid"), reason="POSIX ownership")
def test_an_ancestor_apm_yml_another_user_owns_is_skipped_and_named(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """/tmp/work never adopts another user's /tmp/apm.yml (git's safe.directory class)."""
    import os

    from apm_cli.commands._helpers import _find_apm_yml

    shared = tmp_path / "shared"
    work = shared / "work" / "src"
    work.mkdir(parents=True)
    planted = shared / "apm.yml"
    planted.write_text("name: planted\nversion: 0.0.1\n", encoding="utf-8")
    monkeypatch.setattr(os, "geteuid", lambda: planted.stat().st_uid + 1)

    assert _find_apm_yml(work) is None
    out = " ".join(capsys.readouterr().out.split())
    assert str(planted) in out, out
    assert f"owned by uid {planted.stat().st_uid}" in out, out


@pytest.mark.skipif(not hasattr(__import__("os"), "geteuid"), reason="POSIX ownership")
def test_an_ancestor_apm_yml_the_user_owns_is_found(tmp_path: Path) -> None:
    from apm_cli.commands._helpers import _find_apm_yml

    work = tmp_path / "project" / "src"
    work.mkdir(parents=True)
    manifest = tmp_path / "project" / "apm.yml"
    manifest.write_text("name: mine\nversion: 0.0.1\n", encoding="utf-8")

    assert _find_apm_yml(work) == manifest.resolve()


@pytest.mark.parametrize("package", ["foo", "name@market", "org/repo"])
def test_a_bare_package_argument_is_never_rewritten_from_the_subdirectory(
    tmp_path: Path, package: str
) -> None:
    """Only an explicit ./ or ../ path is relative to the invoking directory (#4545)."""
    from apm_cli.commands._helpers import resolve_cli_packages_for_project_root

    root = tmp_path / "project"
    subdir = root / "sub"
    (subdir / "org").mkdir(parents=True)
    (subdir / package.split("/")[0]).mkdir(exist_ok=True)

    assert resolve_cli_packages_for_project_root((package,), subdir, root) == (package,)
    assert resolve_cli_packages_for_project_root(("./pkg",), subdir, root) == ("./sub/pkg",)


@pytest.mark.parametrize(
    ("invoking", "package", "rebased"),
    [
        ("sub", ".\\pkg", "./sub/pkg"),
        (".packages", "./pkg", "./.packages/pkg"),
        ("sub", "../pkg", "./pkg"),
    ],
    ids=["backslash-prefix", "dot-directory", "parent"],
)
def test_an_explicit_relative_package_is_rebased_onto_the_project_root(
    tmp_path: Path, invoking: str, package: str, rebased: str
) -> None:
    """Rebased paths keep a ./ prefix and POSIX separators (#4545)."""
    from apm_cli.commands._helpers import resolve_cli_packages_for_project_root

    root = tmp_path / "project"
    (root / invoking).mkdir(parents=True)

    assert resolve_cli_packages_for_project_root((package,), root / invoking, root) == (rebased,)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_a_selected_symlink_keeps_its_own_name_when_rebased(tmp_path: Path) -> None:
    """Rebasing never follows the selected link, so install still refuses it (#4545)."""
    from apm_cli.commands._helpers import resolve_cli_packages_for_project_root

    root = tmp_path / "project"
    sub = root / "sub"
    sub.mkdir(parents=True)
    (root / "actual.prompt.md").write_text("# actual\n", encoding="utf-8")
    (sub / "sel.prompt.md").symlink_to("../actual.prompt.md")

    assert resolve_cli_packages_for_project_root(("./sel.prompt.md",), sub, root) == (
        "./sub/sel.prompt.md",
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_an_intermediate_symlink_followed_by_dotdot_lands_where_the_filesystem_does(
    tmp_path: Path,
) -> None:
    """``./link/../pkg`` with ``link -> ../other/inner`` names other/pkg (#4545)."""
    from apm_cli.commands._helpers import resolve_cli_packages_for_project_root

    root = tmp_path / "project"
    sub = root / "sub"
    (root / "other" / "inner").mkdir(parents=True)
    (root / "other" / "pkg").mkdir()
    sub.mkdir()
    (sub / "pkg").mkdir()
    (sub / "link").symlink_to("../other/inner")

    assert resolve_cli_packages_for_project_root(("./link/../pkg",), sub, root) == ("./other/pkg",)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
@pytest.mark.parametrize("spelling", ["./sel.prompt.md/.", "./././sel.prompt.md"])
def test_a_dot_component_never_hides_a_selected_symlink(tmp_path: Path, spelling: str) -> None:
    """``.`` components are dropped before the final one is kept, so the link stays named."""
    from apm_cli.commands._helpers import resolve_cli_packages_for_project_root

    root = tmp_path / "project"
    sub = root / "sub"
    sub.mkdir(parents=True)
    (root / "actual.prompt.md").write_text("# actual\n", encoding="utf-8")
    (sub / "sel.prompt.md").symlink_to("../actual.prompt.md")

    assert resolve_cli_packages_for_project_root((spelling,), sub, root) == ("./sub/sel.prompt.md",)


def test_a_package_on_another_windows_drive_keeps_its_absolute_path(
    tmp_path: Path, monkeypatch
) -> None:
    """With no relative path between drives, discovery keeps going (#4545)."""
    import os

    from apm_cli.commands._helpers import resolve_cli_packages_for_project_root

    root = tmp_path / "project"
    sub = root / "sub"
    sub.mkdir(parents=True)

    def other_drive(path, start=None):
        raise ValueError("path is on mount 'D:', start on mount 'C:'")

    monkeypatch.setattr(os.path, "relpath", other_drive)

    assert resolve_cli_packages_for_project_root(("./pkg",), sub, root) == (
        (sub.resolve() / "pkg").as_posix(),
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_a_symlinked_ancestor_apm_yml_is_named_with_its_target(tmp_path: Path, capsys) -> None:
    """A symlinked ancestor manifest is used, and the notice names its real file (#4545)."""
    from apm_cli.commands._helpers import _find_apm_yml

    project = tmp_path / "project"
    work = project / "src"
    work.mkdir(parents=True)
    target = tmp_path / "shared-apm.yml"
    target.write_text("name: shared\nversion: 0.0.1\n", encoding="utf-8")
    (project / "apm.yml").symlink_to(target)

    assert _find_apm_yml(work) == project / "apm.yml"
    out = " ".join(capsys.readouterr().out.split())
    assert f"a symlink to {target.resolve()}" in out, out


@pytest.mark.parametrize(
    ("invoking", "package", "names"),
    [("sub", "../../", "parent"), ("sub/deeper", "../../", "root")],
    ids=["above-the-root", "the-root-itself"],
)
def test_a_package_naming_the_root_or_above_keeps_an_absolute_path(
    tmp_path: Path, invoking: str, package: str, names: str
) -> None:
    """A rebase to bare ``..`` or ``.`` is refused by the parser, so it stays absolute (#4545)."""
    from apm_cli.commands._helpers import resolve_cli_packages_for_project_root
    from apm_cli.models.dependency.reference import DependencyReference

    root = tmp_path / "project"
    (root / invoking).mkdir(parents=True)
    expected = (root.parent if names == "parent" else root).resolve().as_posix()

    (rebased,) = resolve_cli_packages_for_project_root((package,), root / invoking, root)

    assert rebased == expected
    assert DependencyReference.parse(rebased).local_path == expected
