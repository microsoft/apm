"""Regression: sibling monorepo presets keep their own transitive children.

Issue #3510: when two virtual subdirectory packages from one repository
(``presets/anti-slop`` and ``presets/general``) are both depth-2 entries,
their depth-3 children record ``resolved_by: <owner/repo>``, which matches
both siblings. ``apm deps tree`` and ``apm deps why`` must use each
sibling's installed manifest to attach every child to its declaring preset,
and keep the "could not place" fallback only for genuinely ambiguous graphs.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.models.apm_package import clear_apm_yml_cache

_GLOBAL = "example/dev/presets/global"
_ANTI_SLOP = "example/dev/presets/anti-slop"
_GENERAL = "example/dev/presets/general"
_ANTI_SLOP_CHILDREN = ("acme/humanizer", "acme/plugins/skills/deslop")
_GENERAL_CHILDREN = ("acme/skills/skills/writing", "OtherOrg/planning/skills/planning")

_LOCKFILE = dedent("""\
    lockfile_version: '1'
    dependencies:
    - repo_url: example/dev
      host: github.com
      version: 1.0.0
      virtual_path: presets/global
      is_virtual: true
    - repo_url: example/dev
      host: github.com
      version: 1.0.0
      virtual_path: presets/anti-slop
      is_virtual: true
      depth: 2
      resolved_by: example/dev
    - repo_url: example/dev
      host: github.com
      version: 1.0.0
      virtual_path: presets/general
      is_virtual: true
      depth: 2
      resolved_by: example/dev
    - repo_url: acme/humanizer
      host: github.com
      version: 4.0.0
      depth: 3
      resolved_by: example/dev
    - repo_url: acme/plugins
      host: github.com
      virtual_path: skills/deslop
      is_virtual: true
      depth: 3
      resolved_by: example/dev
    - repo_url: acme/skills
      host: github.com
      virtual_path: skills/writing
      is_virtual: true
      depth: 3
      resolved_by: example/dev
    - repo_url: otherorg/planning
      host: github.com
      virtual_path: skills/planning
      is_virtual: true
      depth: 3
      resolved_by: example/dev
    - repo_url: solo/single
      host: github.com
      version: 2.0.0
    - repo_url: solo/leaf
      host: github.com
      version: 3.0.0
      depth: 2
      resolved_by: solo/single
""")


def _manifest(name: str, deps: tuple[str, ...]) -> str:
    lines = [f"name: {name}", "version: 1.0.0"]
    if deps:
        lines += ["dependencies:", "  apm:", *(f"    - {dep}" for dep in deps)]
    return "\n".join(lines) + "\n"


def _write_package(modules: Path, key: str, deps: tuple[str, ...] = ()) -> None:
    pkg = modules / key
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "apm.yml").write_text(_manifest(key.rsplit("/", 1)[-1], deps), encoding="utf-8")


def _write_fixture(
    apm_dir: Path,
    *,
    anti_slop_children: tuple[str, ...] = _ANTI_SLOP_CHILDREN,
    general_children: tuple[str, ...] = _GENERAL_CHILDREN,
) -> None:
    apm_dir.mkdir(parents=True, exist_ok=True)
    (apm_dir / "apm.yml").write_text(
        _manifest("consumer", (_GLOBAL, "solo/single")), encoding="utf-8"
    )
    (apm_dir / "apm.lock.yaml").write_text(_LOCKFILE, encoding="utf-8")
    modules = apm_dir / "apm_modules"
    _write_package(modules, "example/dev/presets/global", (_GENERAL, _ANTI_SLOP))
    _write_package(modules, "example/dev/presets/anti-slop", anti_slop_children)
    _write_package(modules, "example/dev/presets/general", general_children)
    for key in (
        "acme/humanizer",
        "acme/plugins/skills/deslop",
        "acme/skills/skills/writing",
        "OtherOrg/planning/skills/planning",
        "solo/single",
        "solo/leaf",
    ):
        _write_package(modules, key)
    clear_apm_yml_cache()


def _indent_of(output: str, needle: str) -> int:
    lines = [line for line in output.splitlines() if needle in line]
    assert len(lines) == 1, f"expected one line with {needle!r}:\n{output}"
    line = lines[0]
    return len(line) - len(line.lstrip(" |`+-\u2502\u251c\u2514\u2500"))


def _child_rows_under(output: str, parent: str) -> list[str]:
    """Return the rows rendered one level deeper than *parent*, directly below it."""
    lines = output.splitlines()
    start = next(i for i, line in enumerate(lines) if f"{parent}@" in line)
    depth = _indent_of(output, f"{parent}@")
    rows: list[str] = []
    for line in lines[start + 1 :]:
        stripped = line.lstrip(" |`+-\u2502\u251c\u2514\u2500")
        indent = len(line) - len(stripped)
        if indent <= depth:
            break
        rows.append(stripped)
    return rows


def _invoke(args: list[str]) -> str:
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    return result.output


@pytest.fixture
def user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setenv("COLUMNS", "200")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return home


@pytest.mark.parametrize("scope", ["project", "user"])
def test_tree_attaches_children_to_declaring_sibling_preset(
    scope: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, user_home: Path
) -> None:
    if scope == "user":
        apm_dir = user_home / ".apm"
        args = ["deps", "tree", "-g"]
    else:
        apm_dir = tmp_path / "project"
        apm_dir.mkdir()
        monkeypatch.chdir(apm_dir)
        args = ["deps", "tree"]
    _write_fixture(apm_dir)

    output = _invoke(args)

    assert "could not place in tree" not in output
    anti_slop_rows = " ".join(_child_rows_under(output, _ANTI_SLOP))
    general_rows = " ".join(_child_rows_under(output, _GENERAL))
    assert "acme/humanizer@4.0.0" in anti_slop_rows
    assert "acme/plugins/skills/deslop@" in anti_slop_rows
    assert "acme/skills/skills/writing@" in general_rows
    assert "otherorg/planning/skills/planning@" in general_rows
    assert "acme/humanizer" not in general_rows
    assert "acme/skills/skills/writing" not in anti_slop_rows
    assert _indent_of(output, "acme/humanizer@") > _indent_of(output, f"{_ANTI_SLOP}@")
    assert _indent_of(output, f"{_ANTI_SLOP}@") > _indent_of(output, f"{_GLOBAL}@")
    # A single-package repository renders as before: leaf directly under its parent.
    assert "solo/leaf@3.0.0" in " ".join(_child_rows_under(output, "solo/single"))


@pytest.mark.parametrize("global_flag", [False, True])
def test_why_names_declaring_sibling_preset(
    global_flag: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, user_home: Path
) -> None:
    if global_flag:
        apm_dir = user_home / ".apm"
    else:
        apm_dir = tmp_path / "project"
        apm_dir.mkdir()
        monkeypatch.chdir(apm_dir)
    _write_fixture(apm_dir)
    flag = ["-g"] if global_flag else []

    humanizer = _invoke(["deps", "why", *flag, "acme/humanizer"])
    writing = _invoke(["deps", "why", *flag, "acme/skills/skills/writing"])

    assert [line.strip() for line in humanizer.splitlines()[2:]] == [
        f"{_GLOBAL}   [declared in apm.yml]",
        f"+-- {_ANTI_SLOP}",
        "+-- acme/humanizer",
    ]
    assert f"+-- {_GENERAL}" in writing
    assert _ANTI_SLOP not in writing


def test_genuinely_ambiguous_siblings_keep_fallback_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, user_home: Path
) -> None:
    apm_dir = tmp_path / "project"
    apm_dir.mkdir()
    monkeypatch.chdir(apm_dir)
    # Both siblings declare acme/humanizer: no single declaring parent exists.
    _write_fixture(
        apm_dir,
        general_children=(*_GENERAL_CHILDREN, "acme/humanizer"),
    )

    output = _invoke(["deps", "tree"])

    humanizer_rows = [line for line in output.splitlines() if "acme/humanizer@" in line]
    assert len(humanizer_rows) == 1
    assert "could not place in tree" in humanizer_rows[0]
    assert "acme/skills/skills/writing@" in " ".join(_child_rows_under(output, _GENERAL))


def test_unreadable_sibling_manifest_keeps_fallback_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, user_home: Path
) -> None:
    apm_dir = tmp_path / "project"
    apm_dir.mkdir()
    monkeypatch.chdir(apm_dir)
    _write_fixture(apm_dir)
    (apm_dir / "apm_modules" / "example/dev/presets/general" / "apm.yml").write_text(
        "name: [unclosed\n", encoding="utf-8"
    )
    clear_apm_yml_cache()

    output = _invoke(["deps", "tree"])

    humanizer_rows = [line for line in output.splitlines() if "acme/humanizer@" in line]
    assert len(humanizer_rows) == 1
    assert "could not place in tree" in humanizer_rows[0]
