"""Mutation probes for acquired manifest metadata."""

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_RULE = "install-deployment-manifest-snapshot"


@pytest.mark.parametrize(
    ("rule", "path", "old", "new"),
    [
        (
            SNAPSHOT_RULE,
            "src/apm_cli/install/source_helpers.py",
            "snapshot = APMPackage.from_mapping(",
            "snapshot = APMPackage.from_apm_yml(",
        ),
        (
            SNAPSHOT_RULE,
            "src/apm_cli/install/source_helpers.py",
            "source_path=package.source_path,",
            "source_path=install_path,",
        ),
        (
            SNAPSHOT_RULE,
            "src/apm_cli/install/sources.py",
            "local_pkg = materialized_manifest_snapshot(local_pkg, install_path, pkg_type)",
            "local_pkg = local_pkg",
        ),
        (
            SNAPSHOT_RULE,
            "src/apm_cli/install/sources.py",
            "cached_package = materialized_manifest_snapshot(cached_package, install_path, pkg_type)",
            "cached_package = cached_package",
        ),
        (
            SNAPSHOT_RULE,
            "src/apm_cli/install/sources.py",
            "package_info.package = materialized_manifest_snapshot(",
            "package_info.package = stale_snapshot(",
        ),
        (
            SNAPSHOT_RULE,
            "src/apm_cli/install/source_helpers.py",
            "package_type == PackageType.MARKETPLACE_PLUGIN",
            "False",
        ),
    ],
)
def test_materialized_version_guards_reject_bypasses(
    rule: str, path: str, old: str, new: str
) -> None:
    """A bypass must produce a violation from the specific authority guard."""
    baseline = run_selected_rules(ROOT, (rule,))
    assert baseline.failures == ()
    assert baseline.violations == ()
    source = (ROOT / path).read_text(encoding="utf-8")
    assert old in source

    report = run_selected_rules(ROOT, (rule,), source_overrides={path: source.replace(old, new, 1)})

    assert report.failures == ()
    assert any(violation.rule_id == rule for violation in report.violations)


def test_snapshot_guard_rejects_a_second_metadata_owner() -> None:
    """A second source-specific implementation cannot silently acquire authority."""
    path = "src/apm_cli/install/sources.py"
    source = (ROOT / path).read_text(encoding="utf-8")
    duplicate = (
        "\n\ndef materialized_manifest_snapshot(package, install_path):\n    return package\n"
    )

    report = run_selected_rules(ROOT, (SNAPSHOT_RULE,), source_overrides={path: source + duplicate})

    assert report.failures == ()
    assert any(violation.rule_id == SNAPSHOT_RULE for violation in report.violations)
