"""Contracts keeping public top-level CLI commands discoverable in the docs."""

from pathlib import Path

from apm_cli.cli import cli

REPO_ROOT = Path(__file__).parents[2]
CLI_REFERENCE_DIR = REPO_ROOT / "docs" / "src" / "content" / "docs" / "reference" / "cli"
REFERENCE_INDEX = CLI_REFERENCE_DIR.parent / "index.md"


def _public_command_names() -> set[str]:
    """Return the public top-level command names registered with Click."""
    return {name for name, command in cli.commands.items() if not command.hidden}


def test_public_commands_have_reference_pages() -> None:
    """Require one CLI reference page for every public top-level command."""
    documented_commands = {path.stem for path in CLI_REFERENCE_DIR.glob("*.md")}

    assert _public_command_names() <= documented_commands


def test_public_commands_are_linked_from_reference_index() -> None:
    """Require the reference landing page to link every public command."""
    index = REFERENCE_INDEX.read_text(encoding="utf-8")
    linked_commands = {
        name for name in _public_command_names() if f"[`{name}`](./cli/{name}/)" in index
    }

    assert linked_commands == _public_command_names()
