"""Contracts keeping the public CLI registry aligned with rendered docs."""

from pathlib import Path

from apm_cli.cli import cli
from scripts.check_cli_docs import registry_docs_mismatches

REPO_ROOT = Path(__file__).parents[2]
CLI_REFERENCE_DIR = REPO_ROOT / "docs" / "src" / "content" / "docs" / "reference" / "cli"
REFERENCE_INDEX = CLI_REFERENCE_DIR.parent / "index.md"


def _render_page(dist: Path, name: str) -> None:
    page = dist / "reference" / "cli" / name / "index.html"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("<p>rendered</p>\n", encoding="utf-8")


def _public_command_names(dist: Path) -> set[str]:
    cli_dir = dist / "reference" / "cli"
    cli_dir.mkdir(parents=True, exist_ok=True)
    missing, orphan = registry_docs_mismatches(cli, dist)
    assert orphan == []
    return set(missing)


def _render_public_pages(dist: Path, *, omit: set[str] | None = None) -> set[str]:
    omitted = omit or set()
    public = _public_command_names(dist)
    for name in public - omitted:
        _render_page(dist, name)
    return public


def test_public_commands_are_linked_from_reference_index(tmp_path: Path) -> None:
    """Keep source-level landing-page discoverability separate from rendering."""
    index = REFERENCE_INDEX.read_text(encoding="utf-8")
    public = _public_command_names(tmp_path)
    linked = {name for name in public if f"[`{name}`](./cli/{name}/)" in index}

    assert linked == public


def test_hidden_alias_does_not_require_rendered_page(tmp_path: Path) -> None:
    """The hidden info alias must not create a second documentation contract."""
    public = _render_public_pages(tmp_path)

    missing_pages, orphan_pages = registry_docs_mismatches(cli, tmp_path)

    assert "info" not in public
    assert missing_pages == []
    assert orphan_pages == []


def test_nested_subcommands_share_the_top_level_group_page(tmp_path: Path) -> None:
    """Nested rendered directories do not imply recursive command-page parity."""
    _render_public_pages(tmp_path)
    nested = tmp_path / "reference" / "cli" / "deps" / "tree" / "index.html"
    nested.parent.mkdir(parents=True)
    nested.write_text("<p>nested</p>\n", encoding="utf-8")

    missing_pages, orphan_pages = registry_docs_mismatches(cli, tmp_path)

    assert missing_pages == []
    assert orphan_pages == []


def test_registered_command_without_rendered_page_fails(tmp_path: Path) -> None:
    """Removing one rendered page must identify its executable command."""
    public = _render_public_pages(tmp_path, omit={"doctor"})

    missing_pages, orphan_pages = registry_docs_mismatches(cli, tmp_path)

    assert "doctor" in public
    assert missing_pages == ["doctor"]
    assert orphan_pages == []


def test_rendered_page_without_registered_command_fails(tmp_path: Path) -> None:
    """Adding one rendered page must identify its missing executable."""
    _render_public_pages(tmp_path)
    _render_page(tmp_path, "not-a-command")

    missing_pages, orphan_pages = registry_docs_mismatches(cli, tmp_path)

    assert missing_pages == []
    assert orphan_pages == ["not-a-command"]
