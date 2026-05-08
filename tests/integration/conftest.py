"""Shared fixtures and helpers for integration tests.

Migrating tests should call ``make_copilot_project`` instead of the
legacy ``(project / ".github").mkdir()`` pattern. Under #1154 the bare
``.github/`` directory is no longer a copilot signal -- the file
``.github/copilot-instructions.md`` is required.
"""

from __future__ import annotations

from pathlib import Path


def make_copilot_project(tmp_path: Path, name: str = "test-project") -> Path:
    """Create a temp project with a valid copilot signal.

    Materializes ``<tmp_path>/<name>/.github/copilot-instructions.md`` so
    auto-detection resolves to the copilot target without ambiguity.

    Args:
        tmp_path: pytest ``tmp_path`` fixture.
        name: Project directory name (default ``"test-project"``).

    Returns:
        The created project root.
    """
    project = tmp_path / name
    project.mkdir()
    github_dir = project / ".github"
    github_dir.mkdir()
    (github_dir / "copilot-instructions.md").write_bytes(b"# Copilot instructions\n")
    return project
