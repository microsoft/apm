"""Architecture guardrails for effective marketplace output paths."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def test_effective_marketplace_output_path_has_single_owner() -> None:
    """Generation and drift checking must use the profile-level resolver."""
    root = Path(__file__).parents[2]
    owner = (root / "src/apm_cli/marketplace/output_profiles.py").read_text(encoding="utf-8")
    producer = (root / "src/apm_cli/core/build_orchestrator.py").read_text(encoding="utf-8")
    builder = (root / "src/apm_cli/marketplace/builder.py").read_text(encoding="utf-8")
    drift_check = (root / "src/apm_cli/marketplace/drift_check.py").read_text(encoding="utf-8")
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text(encoding="utf-8")

    assert owner.count("def resolve_effective_output_path(") == 1
    assert "resolve_effective_output_path(" in producer
    assert "resolve_effective_output_path(" in builder
    assert "resolve_effective_output_path(" in drift_check
    assert "Marketplace output paths must route through marketplace/output_profiles.py" in guard


def test_effective_marketplace_output_path_guard_rejects_parallel_decision(
    tmp_path: Path,
) -> None:
    """The boundary lint must reject a second output-path resolution."""
    root = Path(__file__).parents[2]
    sandbox = tmp_path / "repo"
    shutil.copytree(
        root,
        sandbox,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            ".pytest_cache",
            "__pycache__",
            "build",
            "dist",
            "node_modules",
        ),
    )
    drift_check = sandbox / "src/apm_cli/marketplace/drift_check.py"
    drift_check.write_text(
        drift_check.read_text(encoding="utf-8")
        + "\n\ndef _parallel_output_path(config, project_root):\n"
        + "    return project_root / config.claude.output\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        ("bash", "scripts/lint-architecture-boundaries.sh"),
        cwd=sandbox,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )

    assert result.returncode == 1
    assert (
        "Marketplace output paths must route through marketplace/output_profiles.py"
        in result.stdout
    )
