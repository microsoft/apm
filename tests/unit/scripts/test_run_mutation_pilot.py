"""Tests for the bounded mutation-pilot runner."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
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


def _update_plan_owner(pilot: ModuleType) -> Any:
    return next(owner for owner in pilot.OWNERS if owner.key == "update-plan")


def _metadata_path(repo_root: Path, owner: Any) -> Path:
    path = repo_root / "mutants" / f"{owner.source}.meta"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write_metadata(repo_root: Path, owner: Any, payload: object) -> Path:
    path = _metadata_path(repo_root, owner)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


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


@pytest.mark.parametrize("raw_name", ["missing-module", "module.not_mangled"])
def test_canonical_mutant_name_rejects_invalid_mangling(
    pilot: ModuleType,
    raw_name: str,
) -> None:
    with pytest.raises(pilot.PilotError, match="invalid mutmut name"):
        pilot._canonical_mutant_name(raw_name)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("{", "invalid mutmut metadata"),
        ("[]", "root must be an object"),
        ("{}", "exit_code_by_key missing"),
    ],
)
def test_load_exit_codes_rejects_corrupt_or_partial_metadata(
    pilot: ModuleType,
    tmp_path: Path,
    content: str,
    message: str,
) -> None:
    owner = _update_plan_owner(pilot)
    _metadata_path(tmp_path, owner).write_text(content, encoding="utf-8")

    with pytest.raises(pilot.PilotError, match=message):
        pilot._load_exit_codes(owner, tmp_path)


def test_load_exit_codes_rejects_invalid_utf8_metadata(
    pilot: ModuleType,
    tmp_path: Path,
) -> None:
    owner = _update_plan_owner(pilot)
    _metadata_path(tmp_path, owner).write_bytes(b"\xff")

    with pytest.raises(pilot.PilotError, match="invalid mutmut metadata"):
        pilot._load_exit_codes(owner, tmp_path)


@pytest.mark.parametrize("exit_code", ["1", True, 1.5, []])
def test_load_exit_codes_rejects_wrong_exit_code_type(
    pilot: ModuleType,
    tmp_path: Path,
    exit_code: object,
) -> None:
    owner = _update_plan_owner(pilot)
    raw_name = f"{owner.module}.x_build_update_plan__mutmut_1"
    _write_metadata(tmp_path, owner, {"exit_code_by_key": {raw_name: exit_code}})

    with pytest.raises(pilot.PilotError, match="invalid exit code"):
        pilot._load_exit_codes(owner, tmp_path)


def test_load_exit_codes_rejects_canonical_name_collision(
    pilot: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _update_plan_owner(pilot)
    prefix = f"{owner.module}.x_build_update_plan__mutmut_"
    _write_metadata(
        tmp_path,
        owner,
        {"exit_code_by_key": {f"{prefix}1": 1, f"{prefix}2": 1}},
    )
    monkeypatch.setattr(pilot, "_canonical_mutant_name", lambda raw_name: "same.name")

    with pytest.raises(pilot.PilotError, match="duplicate canonical mutant name"):
        pilot._load_exit_codes(owner, tmp_path)


def test_write_report_is_deterministic_atomic_and_printable_ascii(
    pilot: ModuleType,
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "report.json"
    payload = {"z": "snowman \u2603", "a": {"z": 2, "a": 1}}

    pilot._write_report(report_path, payload)
    first = report_path.read_bytes()
    pilot._write_report(
        report_path,
        {"a": {"a": 1, "z": 2}, "z": "snowman \u2603"},
    )

    assert report_path.read_bytes() == first
    assert all(byte < 128 for byte in first)
    assert first == (b'{\n  "a": {\n    "a": 1,\n    "z": 2\n  },\n  "z": "snowman \\u2603"\n}\n')
    assert not (tmp_path / ".report.json.tmp").exists()


def test_write_report_preserves_existing_file_when_atomic_replace_fails(
    pilot: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text("previous\n", encoding="ascii")

    def fail_replace(source: Path, target: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(pilot.PilotError, match="failed to write mutation report"):
        pilot._write_report(report_path, {"status": "accepted"})

    assert report_path.read_text(encoding="ascii") == "previous\n"
    assert not (tmp_path / ".report.json.tmp").exists()


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


def _meta_path(pilot: ModuleType, repo_root: Path, owner: object) -> Path:
    path = repo_root / "mutants" / f"{owner.source}.meta"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write_meta(pilot: ModuleType, repo_root: Path, owner: object, payload: dict) -> Path:
    path = _meta_path(pilot, repo_root, owner)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_load_exit_codes_raises_on_corrupt_json(pilot: ModuleType, tmp_path: Path) -> None:
    owner = next(owner for owner in pilot.OWNERS if owner.key == "update-plan")
    meta_path = _meta_path(pilot, tmp_path, owner)
    meta_path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(pilot.PilotError, match="invalid mutmut metadata"):
        pilot._load_exit_codes(owner, tmp_path)


def test_load_exit_codes_raises_on_missing_exit_code_map(pilot: ModuleType, tmp_path: Path) -> None:
    owner = next(owner for owner in pilot.OWNERS if owner.key == "update-plan")
    _write_meta(pilot, tmp_path, owner, {"not_exit_code_by_key": {}})

    with pytest.raises(pilot.PilotError, match="exit_code_by_key missing"):
        pilot._load_exit_codes(owner, tmp_path)


def test_load_exit_codes_raises_on_wrong_type_exit_code_value(
    pilot: ModuleType, tmp_path: Path
) -> None:
    owner = next(owner for owner in pilot.OWNERS if owner.key == "update-plan")
    raw_name = f"{owner.module}.x_build_update_plan__mutmut_2"
    _write_meta(pilot, tmp_path, owner, {"exit_code_by_key": {raw_name: "0"}})

    with pytest.raises(pilot.PilotError, match="invalid exit code"):
        pilot._load_exit_codes(owner, tmp_path)


def test_load_exit_codes_raises_when_no_mutants_match_owner(
    pilot: ModuleType, tmp_path: Path
) -> None:
    owner = next(owner for owner in pilot.OWNERS if owner.key == "update-plan")
    _write_meta(
        pilot,
        tmp_path,
        owner,
        {"exit_code_by_key": {"apm_cli.other.module.x_unrelated__mutmut_1": 0}},
    )

    with pytest.raises(pilot.PilotError, match="no mutants matched owner allowlist: update-plan"):
        pilot._load_exit_codes(owner, tmp_path)


def test_load_exit_codes_raises_on_malformed_canonical_name(
    pilot: ModuleType, tmp_path: Path
) -> None:
    """A raw mutmut name that matches the owner's glob (thanks to the trailing
    wildcard) but whose final dotted segment lacks the expected 'x_' or
    class-separator prefix is a data-integrity violation in the mutmut
    metadata, not a value the pilot should silently skip or misclassify.
    """
    owner = next(owner for owner in pilot.OWNERS if owner.key == "update-plan")
    raw_name = f"{owner.module}.x_build_update_plan__mutmut_2.stray"
    _write_meta(pilot, tmp_path, owner, {"exit_code_by_key": {raw_name: 0}})

    with pytest.raises(pilot.PilotError, match="invalid mutmut name"):
        pilot._load_exit_codes(owner, tmp_path)


def test_load_exit_codes_raises_on_duplicate_canonical_name(
    pilot: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two distinct mutmut-reported names must never collapse to the same
    canonical identifier; if they did, one mutant outcome would silently
    shadow the other. This pins that data-integrity guard directly, since the
    real mutmut mangling scheme cannot otherwise produce a collision for a
    single owner pattern.
    """
    owner = next(owner for owner in pilot.OWNERS if owner.key == "update-plan")
    raw_a = f"{owner.module}.x_build_update_plan__mutmut_2"
    raw_b = f"{owner.module}.x_build_update_plan__mutmut_3"
    _write_meta(pilot, tmp_path, owner, {"exit_code_by_key": {raw_a: 0, raw_b: 1}})
    monkeypatch.setattr(pilot, "_canonical_mutant_name", lambda raw_name: "collided")

    with pytest.raises(pilot.PilotError, match="duplicate canonical mutant name"):
        pilot._load_exit_codes(owner, tmp_path)


def test_load_exit_codes_scopes_to_owner_patterns_and_decodes_exit_codes(
    pilot: ModuleType, tmp_path: Path
) -> None:
    owner = next(owner for owner in pilot.OWNERS if owner.key == "update-plan")
    matching = f"{owner.module}.x_build_update_plan__mutmut_2"
    unrelated = "apm_cli.other.module.x_unrelated__mutmut_1"
    _write_meta(
        pilot,
        tmp_path,
        owner,
        {"exit_code_by_key": {matching: 0, unrelated: 1}},
    )

    scoped = pilot._load_exit_codes(owner, tmp_path)

    assert scoped == {"apm_cli.install.plan.build_update_plan__mutmut_2": 0}


def test_write_report_writes_sorted_ascii_with_trailing_newline(
    pilot: ModuleType, tmp_path: Path
) -> None:
    report_path = tmp_path / "nested" / "report.json"
    payload = {"b": 1, "a": {"nested_b": 2, "nested_a": 1}}

    pilot._write_report(report_path, payload)

    text = report_path.read_text(encoding="ascii")
    assert text == json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    assert text.endswith("\n")
    assert list(json.loads(text).keys()) == ["a", "b"]
    assert not (report_path.parent / f".{report_path.name}.tmp").exists()


def test_write_report_is_deterministic_across_repeated_writes(
    pilot: ModuleType, tmp_path: Path
) -> None:
    report_path = tmp_path / "report.json"
    payload = {"z": 1, "a": 2}

    pilot._write_report(report_path, payload)
    first = report_path.read_text(encoding="ascii")
    pilot._write_report(report_path, payload)
    second = report_path.read_text(encoding="ascii")

    assert first == second


def test_write_report_cleans_up_temp_file_on_write_failure(
    pilot: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report_path = tmp_path / "report.json"

    def _boom(self: Path, *args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(pilot.Path, "write_text", _boom)

    with pytest.raises(pilot.PilotError, match="failed to write mutation report"):
        pilot._write_report(report_path, {"a": 1})

    assert not (report_path.parent / f".{report_path.name}.tmp").exists()
    assert not report_path.exists()
