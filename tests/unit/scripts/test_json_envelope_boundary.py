"""Guard marketplace and view against duplicate JSON envelope construction."""

from pathlib import Path

import pytest

from scripts.architecture_linter.checks.marketplace_locked_evidence import (
    check_json_envelope,
    check_json_projection,
)
from scripts.architecture_linter.facts import FactsProvider

pytestmark = pytest.mark.component


@pytest.mark.parametrize(
    ("expression", "rejected"),
    [
        ('{"schema_version": 1, "data": []}', True),
        ('{"schema_version": 1, "error": "not_found"}', True),
        ("success_envelope([])", False),
        ('{"schema_version": 1, "other": []}', False),
    ],
)
def test_boundary_requires_shared_envelopes(
    tmp_path: Path, expression: str, rejected: bool
) -> None:
    paths = (
        "src/apm_cli/commands/marketplace/__init__.py",
        "src/apm_cli/commands/view.py",
    )
    for path in paths:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"payload = {expression}\n", encoding="utf-8")

    findings = check_json_envelope(FactsProvider(tmp_path, paths, registry=None))

    assert {finding.path for finding in findings} == (set(paths) if rejected else set())


@pytest.mark.parametrize(
    ("source", "rejected"),
    [
        ("def safe_source(value):\n    return value\n", True),
        ("payload = urlsplit(value)\n", True),
        ("payload = safe_source(value)\n", False),
        ("payload = safe_text(value)\n", False),
    ],
)
def test_boundary_requires_shared_projection(tmp_path: Path, source: str, rejected: bool) -> None:
    """Consumer commands must not establish a second sanitization owner."""
    paths = (
        "src/apm_cli/commands/marketplace/__init__.py",
        "src/apm_cli/commands/view.py",
    )
    for path in paths:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    findings = check_json_projection(FactsProvider(tmp_path, paths, registry=None))
    assert {finding.path for finding in findings} == (set(paths) if rejected else set())
