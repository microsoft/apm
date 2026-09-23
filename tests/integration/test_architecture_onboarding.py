"""Registered static regression guard for metadata-only onboarding."""

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component
ROOT = Path(__file__).parents[2]


@pytest.mark.parametrize(
    "mutation",
    [
        "\nimport shutil\n",
        "\nimport subprocess\n",
        "\nsource.write_text('rewritten')\n",
        "\nrun_install_pipeline()\n",
    ],
)
def test_onboarding_guard_rejects_source_writes_and_execution(mutation: str) -> None:
    """A new converter, source write, or installation path cannot slip in."""
    path = "src/apm_cli/adopt/materialize.py"
    report = run_selected_rules(
        ROOT,
        ("onboarding-metadata-only",),
        source_overrides={path: (ROOT / path).read_text() + mutation},
    )
    assert any(item.rule_id == "onboarding-metadata-only" for item in report.violations)


def test_onboarding_guard_accepts_current_boundary() -> None:
    """The registered full rule has no exceptions outside its manifest writer."""
    report = run_selected_rules(ROOT, ("onboarding-metadata-only",))
    assert not report.violations
    assert not report.failures


def test_onboarding_guard_requires_read_only_admission() -> None:
    """Removing read-only mode must not enable source-normalizing admission."""
    path = "src/apm_cli/adopt/discovery.py"
    original = (ROOT / path).read_text()
    mutated = original.replace(
        "validate_apm_package(path, read_only=True)", "validate_apm_package(path)"
    )
    assert mutated != original
    report = run_selected_rules(
        ROOT, ("onboarding-metadata-only",), source_overrides={path: mutated}
    )
    assert any("read-only package admission" in item.message for item in report.violations)


@pytest.mark.parametrize(
    "mutation",
    [
        "source.write_text('rewritten')",
        "run_install_pipeline()",
        "import shutil",
        "import subprocess",
    ],
)
def test_onboarding_guard_covers_init_discovery_branch(mutation: str) -> None:
    """The public init facade cannot bypass the metadata-only discovery owner."""
    path = "src/apm_cli/commands/init.py"
    original = (ROOT / path).read_text()
    mutated = original.replace(
        "    if discover_flag:\n", f"    if discover_flag:\n        {mutation}\n"
    )
    assert mutated != original
    report = run_selected_rules(
        ROOT, ("onboarding-metadata-only",), source_overrides={path: mutated}
    )
    assert any(
        item.rule_id == "onboarding-metadata-only" and item.path == path
        for item in report.violations
    )
