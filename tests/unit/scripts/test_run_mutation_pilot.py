"""Tests for the bounded mutation-pilot runner."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

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


def test_signal_outcomes_are_classified_accurately(pilot: ModuleType) -> None:
    assert pilot.STATUS_BY_EXIT_CODE[-9] == "terminated"
    assert pilot.STATUS_BY_EXIT_CODE[-11] == "segfault"
    assert {"segfault", "terminated"} <= pilot.FATAL_STATUSES


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


def test_validate_baseline_rejects_tool_version_mismatch(pilot: ModuleType) -> None:
    payload = pilot._baseline_payload(_reports(pilot))
    payload["tool"] = {"name": "mutmut", "version": "9.9.9"}

    with pytest.raises(pilot.BaselineError, match="tool version mismatch"):
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


@pytest.mark.parametrize("status", ["skipped", "timeout", "type_checked"])
def test_incomplete_outcome_fails_baseline_comparison(
    pilot: ModuleType,
    status: str,
) -> None:
    reports = _reports(pilot)
    mutant = f"apm_cli.install.plan.build_update_plan__mutmut_{status}"
    reports["update-plan"]["outcomes"][status] = [mutant]
    reports["update-plan"]["counts"][status] = 1
    reports["update-plan"]["counts"]["total"] += 1
    baseline = pilot._validate_baseline(pilot._baseline_payload(_reports(pilot)))

    comparisons, failed = pilot._compare_with_baseline(reports, baseline)

    assert failed is True
    assert comparisons["update-plan"]["fatal_outcomes"] == {status: [mutant]}


@pytest.mark.parametrize("status", ["skipped", "timeout", "type_checked"])
def test_incomplete_outcome_cannot_be_written_into_baseline(
    pilot: ModuleType,
    status: str,
) -> None:
    reports = _reports(pilot)
    reports["update-plan"]["outcomes"][status] = [
        f"apm_cli.install.plan.build_update_plan__mutmut_{status}"
    ]
    reports["update-plan"]["counts"][status] = 1
    reports["update-plan"]["counts"]["total"] += 1

    with pytest.raises(pilot.PilotError, match="non-survivor failures present"):
        pilot._baseline_payload(reports)


def test_sanitized_environment_removes_credentials_and_preserves_safe_values(
    pilot: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {name: "secret" for name in pilot.SENSITIVE_ENVIRONMENT_NAMES}
    environment["HOME"] = "/safe/home"
    monkeypatch.setattr(pilot.os, "environ", environment)

    sanitized = pilot._sanitized_environment()

    assert set(sanitized) == {"HOME"}
    assert sanitized["HOME"] == "/safe/home"


@pytest.mark.parametrize(
    ("reuse_cache", "expected_removals"),
    [(False, 1), (True, 0)],
)
def test_run_mutmut_respects_cache_mode(
    pilot: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reuse_cache: bool,
    expected_removals: int,
) -> None:
    removals = []
    monkeypatch.setattr(
        pilot.shutil,
        "rmtree",
        lambda path, ignore_errors: removals.append((path, ignore_errors)),
    )
    monkeypatch.setattr(pilot.shutil, "which", lambda name: f"/venv/bin/{name}")
    run = Mock(
        return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(pilot.subprocess, "run", run)

    pilot._run_mutmut(max_children=2, reuse_cache=reuse_cache, repo_root=tmp_path)

    assert len(removals) == expected_removals
    run.assert_called_once()


def test_report_only_skips_mutmut_execution(
    pilot: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reports = _reports(pilot)
    baseline_path = tmp_path / "baseline.json"
    report_path = tmp_path / "report.json"
    pilot.write_baseline(
        baseline_path,
        pilot._baseline_payload(reports),
        label="mutation",
    )
    monkeypatch.setattr(
        pilot,
        "_parse_args",
        lambda: SimpleNamespace(
            baseline=baseline_path,
            max_children=2,
            output=report_path,
            report_only=True,
            reuse_cache=False,
            update_baseline=False,
        ),
    )
    monkeypatch.setattr(
        pilot,
        "_run_mutmut",
        lambda **kwargs: pytest.fail("report-only mode executed mutmut"),
    )
    monkeypatch.setattr(
        pilot,
        "_owner_report",
        lambda owner, repo_root: reports[owner.key],
    )

    result = pilot.main()

    assert result == 0
    assert report_path.is_file()
    assert "metadata only" in capsys.readouterr().out
