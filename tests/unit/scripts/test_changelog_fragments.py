"""CHANGELOG fragments: the check, render and assemble commands on real files.

Fixtures are written in text mode, so on Windows they carry CRLF line endings,
as a Windows editor would save them.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-ci-validation-topology")]

ROOT = Path(__file__).parents[3]
SCRIPT = ROOT / ".apm/skills/cut-release/scripts/changelog_fragments.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("cut_release_changelog_fragments", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fragments = _load()

PREAMBLE = "# Changelog\n\nIntro.\n\n"
PREVIOUS = "## [0.1.0] - 2026-01-01\n\n### Fixed\n\n- Old fix. (#1)\n"
FIXTURE = {
    "4120.fixed.md": "- `apm audit --ci` reports a missing lockfile entry once. (#4120)\n",
    "4107.added.md": "- `apm install --frozen` replays registry packages. (closes #4100, #4107)\n",
    "4133.fixed.md": "- Repository names ending in `g` install again. -- by @someone (#4133)\n",
    "4111.breaking-changes-and-upgrade-notes.md": (
        "- **BREAKING:** `apm pack` drops `--legacy`; use `--format apm`. (#4111)\n"
    ),
    "4150.security.md": "- Release CI refuses tags outside `main`. (#4150)\n",
}
# The block cut-release writes today: section order, one blank line around
# every heading, bullets by PR number within a section.
EXPECTED_BLOCK = """### Breaking changes and upgrade notes

- **BREAKING:** `apm pack` drops `--legacy`; use `--format apm`. (#4111)

### Added

- `apm install --frozen` replays registry packages. (closes #4100, #4107)

### Fixed

- `apm audit --ci` reports a missing lockfile entry once. (#4120)
- Repository names ending in `g` install again. -- by @someone (#4133)

### Security

- Release CI refuses tags outside `main`. (#4150)
"""


def _project(root: Path, unreleased: str = "", files: dict[str, str] = FIXTURE) -> Path:
    (root / "CHANGELOG.md").write_text(
        f"{PREAMBLE}## [Unreleased]\n\n{unreleased}{PREVIOUS}", encoding="utf-8"
    )
    directory = root / "changelog.d"
    directory.mkdir()
    (directory / "README.md").write_text("# fragments\n", encoding="ascii")
    for name, text in files.items():
        (directory / name).write_text(text, encoding="ascii")
    return root


def _check(root: Path, name: str, data: bytes) -> int:
    directory = root / "changelog.d"
    directory.mkdir()
    (directory / name).write_bytes(data)
    return fragments.main(["--root", str(root), "check"])


@pytest.mark.parametrize(
    ("name", "line"),
    [
        ("12.fixed.md", "- Effect. (#12)"),
        ("12.breaking-changes-and-upgrade-notes.md", "- Effect. (closes #3, #12)"),
        ("12.security.md", "- Effect. (#10, #11, #12)"),
        ("12.added.md", "- Effect. -- by @user (#12)"),
    ],
)
@pytest.mark.parametrize("ending", ["", "\n", "\r\n"], ids=["no-eol", "lf", "crlf"])
def test_valid_fragments_pass_check_with_any_line_ending(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str, line: str, ending: str
) -> None:
    assert _check(tmp_path, name, (line + ending).encode("ascii")) == 0

    assert capsys.readouterr().out == "[+] 1 changelog fragment(s) valid\n"


@pytest.mark.parametrize(
    ("name", "text", "problem"),
    [
        ("fix-12.md", "- Effect. (#12)\n", "name must be"),
        ("12.highlights.md", "- Effect. (#12)\n", "unknown section 'highlights'"),
        ("12.fixes.md", "- Effect. (#12)\n", "unknown section 'fixes'"),
        ("12.fixed.md", "- One. (#12)\n- Two. (#12)\n", "exactly one bullet line"),
        ("12.fixed.md", "- One. (#12)\r\n- Two. (#12)\r\n", "exactly one bullet line"),
        ("12.fixed.md", "- One. (#12)\r", "printable ASCII"),
        ("12.fixed.md", "", "exactly one bullet line"),
        ("12.fixed.md", "- Effect \u2014 done. (#12)\n", "printable ASCII"),
        ("12.fixed.md", "Effect. (#12)\n", "single '- ' bullet"),
        ("12.fixed.md", "- Effect. (#12) \n", "single '- ' bullet"),
        ("12.fixed.md", "- Effect. (#13)\n", "(#12)"),
        ("12.fixed.md", "- Effect. (#12, #13)\n", "(#12)"),
        ("12.fixed.md", "- Effect.\n", "(#12)"),
    ],
)
def test_invalid_fragments_name_the_broken_rule(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str, text: str, problem: str
) -> None:
    assert _check(tmp_path, name, text.encode("utf-8")) == 1

    out = capsys.readouterr().out
    assert out.startswith(f"[x] changelog.d/{name}: ")
    assert problem in out


def _render(root: Path, capsys: pytest.CaptureFixture[str], *extra: str) -> str:
    assert fragments.main(["--root", str(root), "render", *extra]) == 0
    return capsys.readouterr().out


def test_render_of_fixture_fragments_matches_todays_block(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)

    assert _render(root, capsys) == EXPECTED_BLOCK
    assert _render(root, capsys, "--version", "0.2.0", "--date", "2026-02-02") == (
        f"## [0.2.0] - 2026-02-02\n\n{EXPECTED_BLOCK}"
    )


def test_render_puts_legacy_unreleased_bullets_before_fragments(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    legacy = "### Fixed\n\n- Legacy fix. (#4000)\n\n### Removed\n\n- Legacy removal. (#4001)\n\n"
    root = _project(tmp_path, legacy, {"4120.fixed.md": FIXTURE["4120.fixed.md"]})

    assert _render(root, capsys) == (
        "### Removed\n\n- Legacy removal. (#4001)\n\n"
        "### Fixed\n\n- Legacy fix. (#4000)\n"
        "- `apm audit --ci` reports a missing lockfile entry once. (#4120)\n"
    )


def test_assemble_writes_the_version_block_and_deletes_fragments(tmp_path: Path) -> None:
    root = _project(tmp_path, "### Changed\n\n- Legacy change. (#4001)\n\n")

    removed = fragments.assemble(root, "0.2.0", "2026-02-02")

    assert sorted(path.name for path in removed) == sorted(FIXTURE)
    assert [path.name for path in (root / "changelog.d").iterdir()] == ["README.md"]
    block = EXPECTED_BLOCK.replace(
        "### Fixed", "### Changed\n\n- Legacy change. (#4001)\n\n### Fixed"
    )
    assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == (
        f"{PREAMBLE}## [Unreleased]\n\n## [0.2.0] - 2026-02-02\n\n{block}\n{PREVIOUS}"
    )
    assert b"\r" not in (root / "CHANGELOG.md").read_bytes()


@pytest.mark.parametrize(
    ("version", "date", "extra", "message"),
    [
        ("0.1.0", "2026-02-02", {}, "already has"),
        ("v0.2.0", "2026-02-02", {}, "X.Y.Z"),
        ("0.2.0", "02/02/2026", {}, "YYYY-MM-DD"),
        ("0.2.0", "2026-02-02", {"9.fixed.md": "- Two\n- lines (#9)\n"}, "9.fixed.md"),
    ],
)
def test_assemble_refuses_without_writing(
    tmp_path: Path, version: str, date: str, extra: dict[str, str], message: str
) -> None:
    root = _project(tmp_path, files={**FIXTURE, **extra})
    before = (root / "CHANGELOG.md").read_text(encoding="utf-8")

    with pytest.raises(fragments.ChangelogError, match=message):
        fragments.assemble(root, version, date)

    assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == before
    assert len(list((root / "changelog.d").iterdir())) == len(FIXTURE) + len(extra) + 1


def test_check_command_reports_every_bad_fragment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path, files={"5.fixed.md": "- Ok. (#6)\n", "x.md": "- Ok. (#7)\n"})

    assert fragments.main(["--root", str(root), "check"]) == 1

    lines = capsys.readouterr().out.splitlines()
    assert [line.split(":")[0] for line in lines] == [
        "[x] changelog.d/5.fixed.md",
        "[x] changelog.d/x.md",
    ]
