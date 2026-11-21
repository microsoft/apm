"""CHANGELOG fragments: check, render, assemble and the [Unreleased] edit rule."""

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


@pytest.mark.parametrize(
    "name",
    ["12.fixed.md", "12.breaking-changes-and-upgrade-notes.md", "12.security.md"],
)
def test_valid_fragment_names_and_reference_shapes(name: str) -> None:
    for line in (
        "- Effect. (#12)",
        "- Effect. (closes #3, #12)",
        "- Effect. (#10, #11, #12)",
        "- Effect. -- by @user (#12)",
    ):
        assert fragments.fragment_problems(name, line + "\n") == []


@pytest.mark.parametrize(
    ("name", "text", "problem"),
    [
        ("fix-12.md", "- Effect. (#12)\n", "name must be"),
        ("12.highlights.md", "- Effect. (#12)\n", "unknown section 'highlights'"),
        ("12.fixes.md", "- Effect. (#12)\n", "unknown section 'fixes'"),
        ("12.fixed.md", "- One. (#12)\n- Two. (#12)\n", "exactly one bullet line"),
        ("12.fixed.md", "", "exactly one bullet line"),
        ("12.fixed.md", "- Effect \u2014 done. (#12)\n", "printable ASCII"),
        ("12.fixed.md", "Effect. (#12)\n", "single '- ' bullet"),
        ("12.fixed.md", "- Effect. (#12) \n", "single '- ' bullet"),
        ("12.fixed.md", "- Effect. (#13)\n", "(#12)"),
        ("12.fixed.md", "- Effect. (#12, #13)\n", "(#12)"),
        ("12.fixed.md", "- Effect.\n", "(#12)"),
    ],
)
def test_invalid_fragments_name_the_broken_rule(name: str, text: str, problem: str) -> None:
    problems = fragments.fragment_problems(name, text)
    assert any(problem in found for found in problems), problems


def test_render_of_fixture_fragments_matches_todays_block(tmp_path: Path) -> None:
    root = _project(tmp_path)

    assert fragments.release_block(root) == EXPECTED_BLOCK
    assert fragments.release_block(root, "0.2.0", "2026-02-02") == (
        f"## [0.2.0] - 2026-02-02\n\n{EXPECTED_BLOCK}"
    )


def test_render_puts_legacy_unreleased_bullets_before_fragments(tmp_path: Path) -> None:
    legacy = "### Fixed\n\n- Legacy fix. (#4000)\n\n### Removed\n\n- Legacy removal. (#4001)\n\n"
    root = _project(tmp_path, legacy, {"4120.fixed.md": FIXTURE["4120.fixed.md"]})

    assert fragments.release_block(root) == (
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
    assert fragments.unreleased_bullets((root / "CHANGELOG.md").read_text()) == []


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


def _changelog(*unreleased: str) -> str:
    return PREAMBLE + "## [Unreleased]\n\n### Fixed\n\n" + "".join(unreleased) + "\n" + PREVIOUS


@pytest.mark.parametrize(
    ("base", "head", "added"),
    [
        (_changelog("- A. (#1)\n"), _changelog("- A. (#1)\n", "- B. (#2)\n"), ["- B. (#2)"]),
        (_changelog("- A. (#1)\n"), _changelog("- A, reworded. (#1)\n"), []),
        (_changelog("- A. (#1)\n"), _changelog(), []),
        (_changelog("- A. (#1)\n"), _changelog("- A. (#1)\n"), []),
        (_changelog(), _changelog("- A. (#1)\n", "- A. (#1)\n"), ["- A. (#1)", "- A. (#1)"]),
        (_changelog(), _changelog().replace("- Old fix. (#1)", "- Old fix. (#1)\n- New."), []),
        (PREAMBLE + PREVIOUS, _changelog("- A. (#1)\n"), ["- A. (#1)"]),
    ],
    ids=["added", "reworded", "released", "unchanged", "duplicate", "older-block", "new-head"],
)
def test_unreleased_rule_fails_only_on_net_new_bullets(
    base: str, head: str, added: list[str]
) -> None:
    assert fragments.new_unreleased_bullets(base, head) == added
