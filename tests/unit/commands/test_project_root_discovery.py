"""Project root discovery from a subdirectory (#4545): ownership and path rebasing."""

from __future__ import annotations

import os
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


# -- $HOME is a discovery ceiling (#5448) --


def _home_world(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    home = tmp_path / "home"
    work = home / "work" / "newproj"
    work.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home, work


def test_an_unresolved_home_keeps_discovery_in_the_start_directory(
    tmp_path: Path, monkeypatch
) -> None:
    from apm_cli.commands._helpers import _find_apm_yml

    home, work = _home_world(tmp_path, monkeypatch)
    (home / "apm.yml").write_text("name: home\nversion: 1.0.0\n", encoding="utf-8")

    def no_home(cls) -> Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", classmethod(no_home))

    assert _find_apm_yml(work) is None
    assert (home / "apm.yml").read_text(encoding="utf-8") == "name: home\nversion: 1.0.0\n"


def test_a_walk_below_home_never_adopts_the_home_manifest(tmp_path: Path, monkeypatch) -> None:
    from apm_cli.commands._helpers import _find_apm_yml

    home, work = _home_world(tmp_path, monkeypatch)
    (home / "apm.yml").write_text("name: home\nversion: 1.0.0\n", encoding="utf-8")

    assert _find_apm_yml(work) is None


def test_a_manifest_between_home_and_the_start_is_still_found(tmp_path: Path, monkeypatch) -> None:
    from apm_cli.commands._helpers import _find_apm_yml

    home, work = _home_world(tmp_path, monkeypatch)
    (home / "apm.yml").write_text("name: home\nversion: 1.0.0\n", encoding="utf-8")
    project = home / "work" / "apm.yml"
    project.write_text("name: mid\nversion: 1.0.0\n", encoding="utf-8")

    assert _find_apm_yml(work) == project.resolve()


def test_a_project_rooted_exactly_at_home_still_works(tmp_path: Path, monkeypatch) -> None:
    from apm_cli.commands._helpers import _find_apm_yml

    home, _ = _home_world(tmp_path, monkeypatch)
    (home / "apm.yml").write_text("name: home\nversion: 1.0.0\n", encoding="utf-8")

    assert _find_apm_yml(home) == (home / "apm.yml").resolve()


def test_a_walk_outside_home_still_adopts_an_ancestor_manifest(tmp_path: Path, monkeypatch) -> None:
    from apm_cli.commands._helpers import _find_apm_yml

    _home_world(tmp_path, monkeypatch)
    outside = tmp_path / "elsewhere"
    work = outside / "src"
    work.mkdir(parents=True)
    (outside / "apm.yml").write_text("name: o\nversion: 1.0.0\n", encoding="utf-8")

    assert _find_apm_yml(work) == (outside / "apm.yml").resolve()


def test_a_symlinked_home_is_a_ceiling_too(tmp_path: Path, monkeypatch) -> None:
    from apm_cli.commands._helpers import _find_apm_yml

    real = tmp_path / "real-home"
    work = real / "work" / "newproj"
    work.mkdir(parents=True)
    link = tmp_path / "linked-home"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    monkeypatch.setenv("HOME", str(link))
    monkeypatch.setenv("USERPROFILE", str(link))
    (real / "apm.yml").write_text("name: home\nversion: 1.0.0\n", encoding="utf-8")

    assert _find_apm_yml(work) is None
    assert _find_apm_yml(link / "work" / "newproj") is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows resolves home from USERPROFILE")
def test_a_walk_below_userprofile_never_adopts_the_home_manifest(
    tmp_path: Path, monkeypatch
) -> None:
    from apm_cli.commands._helpers import _find_apm_yml

    home = tmp_path / "profile"
    work = home / "work" / "newproj"
    work.mkdir(parents=True)
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setenv("USERPROFILE", str(home))
    (home / "apm.yml").write_text("name: home\nversion: 1.0.0\n", encoding="utf-8")

    assert _find_apm_yml(work) is None


# -- the ceiling is #5470's HOME-or-above predicate, by identity (#4545) --


def test_a_walk_never_adopts_a_manifest_above_home_even_outside_home(
    tmp_path: Path, monkeypatch
) -> None:
    from apm_cli.commands._helpers import _find_apm_yml

    home = tmp_path / "base" / "home"
    home.mkdir(parents=True)
    work = tmp_path / "base" / "other" / "sub"
    work.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    (tmp_path / "base" / "apm.yml").write_text("name: above\nversion: 1.0.0\n", encoding="utf-8")
    project = tmp_path / "base" / "other" / "apm.yml"

    assert _find_apm_yml(work) is None
    project.write_text("name: p\nversion: 1.0.0\n", encoding="utf-8")
    assert _find_apm_yml(work) == project.resolve()


def test_a_walk_from_home_never_adopts_a_manifest_above_home(tmp_path: Path, monkeypatch) -> None:
    from apm_cli.commands._helpers import _find_apm_yml

    home = tmp_path / "base" / "home"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    (tmp_path / "base" / "apm.yml").write_text("name: above\nversion: 1.0.0\n", encoding="utf-8")

    assert _find_apm_yml(home) is None


def _case_variant(path: Path) -> Path | None:
    """The same directory spelled in another case, or None on a case-sensitive FS."""
    variant = path.with_name(path.name.swapcase())
    return variant if variant != path and variant.exists() else None


def test_a_case_variant_home_is_still_the_ceiling(tmp_path: Path, monkeypatch) -> None:
    from apm_cli.commands._helpers import _find_apm_yml, ensure_project_root_is_not_user_scope

    home = tmp_path / "Home"
    work = home / "work" / "newproj"
    work.mkdir(parents=True)
    variant = _case_variant(home)
    if variant is None:
        pytest.skip("case-sensitive filesystem")
    monkeypatch.setenv("HOME", str(variant))
    monkeypatch.setenv("USERPROFILE", str(variant))
    (home / "apm.yml").write_text("name: home\nversion: 1.0.0\n", encoding="utf-8")

    assert _find_apm_yml(work) is None
    ensure_project_root_is_not_user_scope(home, command="install")  # existing project: warns


# -- a new project at HOME is refused; an existing one warns (#4545) --


def test_the_scope_guard_refuses_a_new_project_at_home_and_names_the_fix(
    tmp_path: Path, monkeypatch
) -> None:
    import click

    from apm_cli.commands._helpers import ensure_project_root_is_not_user_scope

    home, work = _home_world(tmp_path, monkeypatch)

    with pytest.raises(click.UsageError, match=r"your project directory.*apm install -g"):
        ensure_project_root_is_not_user_scope(home)
    with pytest.raises(click.UsageError, match=r"apm install -g.*apm install --root <dir>"):
        ensure_project_root_is_not_user_scope(home)
    for command in ("update", "lock", "uninstall"):
        ensure_project_root_is_not_user_scope(home, command=command)  # never creates a project
    ensure_project_root_is_not_user_scope(work)
    ensure_project_root_is_not_user_scope(home, user_scope=True)


def test_the_scope_guard_warns_once_for_an_existing_home_project(
    tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from apm_cli.commands._helpers import ensure_project_root_is_not_user_scope

    home, _ = _home_world(tmp_path, monkeypatch)
    (home / "apm.yml").write_text("name: home\nversion: 1.0.0\n", encoding="utf-8")

    ensure_project_root_is_not_user_scope(home, command="lock")

    out = " ".join(capsys.readouterr().out.split())
    assert out.count("[!]") == 1 and "apm lock -g" in out, out


# -- the implicit walk stops at the user metadata root (#5448 class) --


@pytest.mark.parametrize("override", [False, True], ids=["default-apm-dir", "apm-home-override"])
def test_an_implicit_walk_never_adopts_the_global_manifest(
    tmp_path: Path, monkeypatch, override: bool
) -> None:
    from apm_cli.commands._helpers import _find_apm_yml

    home, _ = _home_world(tmp_path, monkeypatch)
    metadata = home / "custom-apm" if override else home / ".apm"
    scratch = metadata / "cache" / "scratch"
    scratch.mkdir(parents=True)
    (metadata / "apm.yml").write_text("name: global\nversion: 1.0.0\n", encoding="utf-8")
    if override:
        monkeypatch.setenv("APM_HOME", str(metadata))
    monkeypatch.chdir(scratch)

    assert _find_apm_yml() is None
    assert _find_apm_yml(scratch) == (metadata / "apm.yml").resolve()  # explicit start unchanged


def test_a_plugin_json_source_package_is_not_pinned_as_a_bundle(
    tmp_path: Path, monkeypatch
) -> None:
    from apm_cli.commands._helpers import resolve_cli_packages_for_project_root

    root, sub = tmp_path / "proj", tmp_path / "proj" / "sub"
    plugin = sub / "pkg"
    plugin.mkdir(parents=True)
    (plugin / "plugin.json").write_text("{}", encoding="utf-8")
    (plugin / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n", encoding="utf-8")
    bundle = sub / "bundle.zip"
    bundle.write_bytes(b"PK")

    assert resolve_cli_packages_for_project_root(("pkg", "bundle.zip"), sub, root) == (
        "pkg",
        str(bundle.resolve()),
    )


def test_the_metadata_root_stop_compares_by_identity(tmp_path: Path, monkeypatch) -> None:
    from apm_cli.commands._helpers import _find_apm_yml

    home, _ = _home_world(tmp_path, monkeypatch)
    metadata = home / "real-apm"
    scratch = metadata / "cache" / "scratch"
    scratch.mkdir(parents=True)
    (metadata / "apm.yml").write_text("name: global\nversion: 1.0.0\n", encoding="utf-8")
    alias = home / "alias-apm"
    alias.symlink_to(metadata, target_is_directory=True)
    monkeypatch.setenv("APM_HOME", str(alias))
    monkeypatch.chdir(scratch)

    assert _find_apm_yml() is None


def test_a_failed_home_resolution_is_not_remembered(tmp_path: Path, monkeypatch) -> None:
    from apm_cli.utils import executable_exclusion as ee

    home, work = _home_world(tmp_path, monkeypatch)
    ee._RESOLVED_HOME.clear()
    real = Path.resolve
    calls = {"n": 0}

    def flaky(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", flaky)

    assert ee.resolved_home() is None
    resolved = ee.resolved_home()
    assert resolved == home.resolve()
    assert ee.is_home_or_above(home.parent, resolved)
    assert not ee.is_home_or_above(work, resolved)
    assert not ee.is_home_or_above(home, None)


def test_a_failed_chain_stat_still_treats_home_ancestors_as_a_ceiling(
    tmp_path: Path, monkeypatch
) -> None:
    from apm_cli.commands._helpers import _find_apm_yml
    from apm_cli.utils import executable_exclusion as ee

    home, work = _home_world(tmp_path, monkeypatch)
    above = tmp_path / "apm.yml"
    above.write_text("name: above\nversion: 1.0.0\n", encoding="utf-8")
    ee._HOME_CHAIN.clear()
    real_stat = os.stat

    def failing(path, *args, **kwargs):
        if Path(path) == home:
            raise OSError("stat failed")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(ee.os, "stat", failing)

    assert _find_apm_yml(work) is None
    assert above.read_text(encoding="utf-8") == "name: above\nversion: 1.0.0\n"
    assert not (tmp_path / "apm.lock.yaml").exists()


def test_an_unreadable_home_level_is_still_a_ceiling(tmp_path: Path, monkeypatch) -> None:
    from apm_cli.commands._helpers import _find_apm_yml
    from apm_cli.utils import executable_exclusion as ee

    home, work = _home_world(tmp_path, monkeypatch)
    (home / "apm.yml").write_text("name: home\nversion: 1.0.0\n", encoding="utf-8")
    (tmp_path / "apm.yml").write_text("name: above\nversion: 1.0.0\n", encoding="utf-8")
    ee._HOME_CHAIN.clear()
    real_stat = os.stat

    def denied(path, *args, **kwargs):
        if Path(path) in (home, tmp_path):
            raise PermissionError("denied")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(ee.os, "stat", denied)

    assert _find_apm_yml(work) is None
    assert ee.is_home_or_above(tmp_path, home)


def test_the_home_identity_stats_do_not_grow_with_walk_depth(tmp_path: Path, monkeypatch) -> None:
    from apm_cli.commands._helpers import _find_apm_yml
    from apm_cli.utils import executable_exclusion as ee

    home, shallow = _home_world(tmp_path, monkeypatch)
    deep = home / "work" / "d1" / "d2" / "d3" / "d4" / "d5"
    deep.mkdir(parents=True)
    (home / "work" / "apm.yml").write_text("name: top\nversion: 1.0.0\n", encoding="utf-8")
    home_chain = {home, *home.resolve().parents, home.resolve()}
    real_stat = os.stat

    def chain_stats(start: Path) -> list[Path]:
        ee._HOME_CHAIN.clear()
        ee._RESOLVED_HOME.clear()
        ee._LEVEL_VERDICTS.clear()
        seen: list[Path] = []

        def counting(path, *args, **kwargs):
            seen.append(Path(path))
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(ee.os, "stat", counting)
        try:
            assert _find_apm_yml(start) == (home / "work" / "apm.yml").resolve()
        finally:
            monkeypatch.setattr(ee.os, "stat", real_stat)
        return [path for path in seen if path in home_chain]

    one_level, five_levels = chain_stats(shallow), chain_stats(deep)

    assert len(one_level) == len(five_levels)
