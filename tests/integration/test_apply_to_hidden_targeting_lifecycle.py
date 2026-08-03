"""Lifecycle coverage for YAML-list applyTo placement in hidden tool trees."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_e2e_mode,
    pytest.mark.requires_apm_binary,
]

_SCENARIO_ID = "apply-to-hidden-directory-placement"
_PATTERNS = (
    ".opencode/**/project,guide.md",
    ".github/**/*.md",
    "**/.apm/**/*.md",
)


def _write_project(project: Path) -> None:
    """Create one project whose rule targets supported hidden tool trees."""
    (project / "apm.yml").write_text(
        "name: hidden-targeting\nversion: 0.1.0\n",
        encoding="utf-8",
    )
    instructions = project / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    (instructions / "hidden-tool-rule.instructions.md").write_text(
        "---\n"
        "description: Hidden tool tree conventions\n"
        "applyTo:\n"
        + "".join(f"  - '{pattern}'\n" for pattern in _PATTERNS)
        + "---\n\n# Hidden tool conventions\n\nKeep tool metadata consistent.\n",
        encoding="utf-8",
    )
    for relative_path in (
        ".opencode/project,guide.md",
        ".github/contributing.md",
        ".apm/context/project.md",
    ):
        path = project / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("target\n", encoding="utf-8")


def test_apply_to_hidden_directory_placement_is_idempotent(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Two OpenCode compiles preserve list patterns and avoid fallback placement."""
    project = tmp_path / "hidden-targeting"
    project.mkdir()
    _write_project(project)
    runner = ApmLifecycleRunner((str(apm_binary_path),))

    first = runner.run(
        ("compile", "--target", "opencode"),
        scenario_id=_SCENARIO_ID,
        cwd=project,
        env=dict(os.environ),
    )
    assert first.returncode == 0, first.stderr or first.stdout
    generated = project / "AGENTS.md"
    assert generated.is_file(), (
        "matching hidden files must produce a distributed placement\n"
        f"stdout:\n{first.stdout}\nstderr:\n{first.stderr}"
    )
    first_bytes = generated.read_bytes()
    assert first_bytes
    second = runner.run(
        ("compile", "--target", "opencode"),
        scenario_id=_SCENARIO_ID,
        cwd=project,
        env=dict(os.environ),
    )
    assert second.returncode == 0, second.stderr or second.stdout
    assert generated.read_bytes() == first_bytes
    source = (project / ".apm" / "instructions" / "hidden-tool-rule.instructions.md").read_text(
        encoding="utf-8"
    )
    assert all(f"  - '{pattern}'" in source for pattern in _PATTERNS)
    combined_output = "\n".join(f"{result.stdout}\n{result.stderr}" for result in (first, second))
    assert "matched no files" not in combined_output
