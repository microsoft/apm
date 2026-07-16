"""Tests for the bounded mutation-pilot runner."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
import tomllib

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "run_mutation_pilot.py"


def _load_module() -> ModuleType:
    scripts_dir = str(SCRIPT_PATH.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location("run_mutation_pilot", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def pilot() -> ModuleType:
    return _load_module()


def _reports(pilot: ModuleType, *, update_survivors: list[str] | None = None) -> dict:
    reports = {}
    for owner in pilot.OWNERS:
        survivors = update_survivors if owner.key == "update-plan" else []
        reports[owner.key] = {
            "counts": {
                "killed": 1,
                "survived": len(survivors or []),
                "total": 1 + len(survivors or []),
            },
            "functions": list(owner.functions),
            "outcomes": {
                "killed": [f"{owner.module}.killed__mutmut_1"],
                "survived": survivors or [],
            },
            "source": owner.source,
            "test_seams": list(owner.test_seams),
        }
    return reports


def test_method_pattern_is_exact_and_internal_separator_stays_runtime_only(
    pilot: ModuleType,
) -> None:
    owner = next(owner for owner in pilot.OWNERS if owner.key == "link-projection")

    assert owner.patterns == (
        "apm_cli.compilation.link_resolver."
        f"x{chr(0x01C1)}UnifiedLinkResolver{chr(0x01C1)}_resolve_in_package_asset_link*",
    )


def test_canonical_mutant_name_removes_internal_mangling(pilot: ModuleType) -> None:
    separator = chr(0x01C1)

    assert (
        pilot._canonical_mutant_name(
            f"apm_cli.compilation.link_resolver."
            f"x{separator}UnifiedLinkResolver{separator}_resolve__mutmut_4"
        )
        == "apm_cli.compilation.link_resolver.UnifiedLinkResolver._resolve__mutmut_4"
    )
    assert (
        pilot._canonical_mutant_name("apm_cli.install.plan.x_build_update_plan__mutmut_2")
        == "apm_cli.install.plan.build_update_plan__mutmut_2"
    )


def test_mutmut_config_matches_canonical_owner_scope(pilot: ModuleType) -> None:
    config = tomllib.loads((pilot.REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    mutmut = config["tool"]["mutmut"]

    assert mutmut["only_mutate"] == [owner.source for owner in pilot.OWNERS]
    assert mutmut["pytest_add_cli_args_test_selection"] == [
        seam for owner in pilot.OWNERS for seam in owner.test_seams
    ]


def test_validate_baseline_rejects_schema_version_mismatch(pilot: ModuleType) -> None:
    payload = pilot._baseline_payload(_reports(pilot))
    payload["schema_version"] = 99

    with pytest.raises(pilot.BaselineError, match="unsupported schema_version"):
        pilot._validate_baseline(payload)


def test_new_survivor_fails_baseline_comparison(pilot: ModuleType) -> None:
    reports = _reports(
        pilot,
        update_survivors=["apm_cli.install.plan.build_update_plan__mutmut_2"],
    )
    baseline = pilot._validate_baseline(pilot._baseline_payload(_reports(pilot)))

    comparisons, failed = pilot._compare_with_baseline(reports, baseline)

    assert failed is True
    assert comparisons["update-plan"]["unexpected_survivors"] == [
        "apm_cli.install.plan.build_update_plan__mutmut_2"
    ]


def test_killed_accepted_survivor_is_reported_without_failing(pilot: ModuleType) -> None:
    survivor = "apm_cli.install.plan.build_update_plan__mutmut_2"
    baseline = pilot._validate_baseline(
        pilot._baseline_payload(_reports(pilot, update_survivors=[survivor]))
    )

    comparisons, failed = pilot._compare_with_baseline(_reports(pilot), baseline)

    assert failed is False
    assert comparisons["update-plan"]["resolved_survivors"] == [survivor]


def test_timeout_cannot_be_written_into_baseline(pilot: ModuleType) -> None:
    reports = _reports(pilot)
    reports["update-plan"]["outcomes"]["timeout"] = [
        "apm_cli.install.plan.build_update_plan__mutmut_9"
    ]
    reports["update-plan"]["counts"]["timeout"] = 1
    reports["update-plan"]["counts"]["total"] += 1

    with pytest.raises(pilot.PilotError, match="non-survivor failures present"):
        pilot._baseline_payload(reports)
