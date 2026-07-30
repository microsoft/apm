"""Architecture guardrails for the marketplace tag-pattern owner."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def test_marketplace_tag_pattern_has_single_owner() -> None:
    """Producer, parser, and resolver must consume one validation owner."""
    root = Path(__file__).parents[2]
    owner = (root / "src/apm_cli/marketplace/tag_pattern.py").read_text()
    producer = (root / "src/apm_cli/marketplace/yml_schema.py").read_text()
    consumer = (root / "src/apm_cli/marketplace/models.py").read_text()
    resolver = (root / "src/apm_cli/marketplace/version_resolver.py").read_text()
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text()

    assert owner.count("def validate_tag_pattern(") == 1
    assert "validate_tag_pattern(pattern, context=context)" in producer
    assert "tag_pattern = validate_tag_pattern(" in consumer
    assert "tag_pattern = validate_tag_pattern(tag_pattern)" in resolver
    assert "Marketplace tag patterns must route through marketplace/tag_pattern.py" in guard


def test_marketplace_tag_pattern_guard_rejects_parallel_validation(
    tmp_path: Path,
) -> None:
    """The boundary lint must reject a second placeholder validator."""
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
    models_path = sandbox / "src/apm_cli/marketplace/models.py"
    models_source = models_path.read_text(encoding="utf-8")
    models_path.write_text(
        models_source
        + "\n\ndef _parallel_pattern_check(pattern: str) -> bool:\n"
        + '    return "{version}" in pattern\n',
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
    assert "Marketplace tag patterns must route through marketplace/tag_pattern.py" in result.stdout
