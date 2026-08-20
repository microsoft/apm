"""Tests for the bounded mutation-pilot runner."""

from __future__ import annotations

import importlib.util
import json
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


def test_lockfile_owner_covers_field_normalizer_patterns(
    pilot: ModuleType,
) -> None:
    """The lockfile-normalization owner targets the two fail-closed
    normalizers only, NOT LockedDependency's dataclass methods
    (to_dict/from_dict/to_dependency_ref).

    mutmut 3.6.0 never mutates methods of a `@dataclass`-decorated class
    (it skips the whole ClassDef when it carries any decorator), and both
    LockedDependency and LockFile are `@dataclass`. The normalizers below
    are bare module-level functions invoked from
    `LockedDependency.from_dict` -- they are the only mutation-viable seam
    this owner can reach; the decorated dataclass methods remain defended
    only by PR #2246's seven manual mutation-break twins, per the
    follow-up constraint recorded on this owner in
    scripts/run_mutation_pilot.py.
    """
    owner = next(owner for owner in pilot.OWNERS if owner.key == "lockfile-normalization")

    assert owner.functions == (
        "_normalize_lockfile_host_type",
        "_normalize_exec_status",
    )
    assert owner.patterns == (
        "apm_cli.deps.lockfile.x__normalize_lockfile_host_type*",
        "apm_cli.deps.lockfile.x__normalize_exec_status*",
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


def test_canonical_mutant_name_rejects_name_without_separator(pilot: ModuleType) -> None:
    with pytest.raises(pilot.PilotError, match="invalid mutmut name"):
        pilot._canonical_mutant_name("missing-module")


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


def _write_meta(pilot: ModuleType, repo_root: Path, owner: object, payload: object) -> Path:
    path = _meta_path(pilot, repo_root, owner)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_load_exit_codes_raises_on_corrupt_json(pilot: ModuleType, tmp_path: Path) -> None:
    owner = next(owner for owner in pilot.OWNERS if owner.key == "update-plan")
    meta_path = _meta_path(pilot, tmp_path, owner)
    meta_path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(pilot.PilotError, match="invalid mutmut metadata"):
        pilot._load_exit_codes(owner, tmp_path)


def test_load_exit_codes_raises_on_non_object_root(
    pilot: ModuleType,
    tmp_path: Path,
) -> None:
    owner = next(owner for owner in pilot.OWNERS if owner.key == "update-plan")
    _write_meta(pilot, tmp_path, owner, [])

    with pytest.raises(pilot.PilotError, match="root must be an object"):
        pilot._load_exit_codes(owner, tmp_path)


def test_load_exit_codes_raises_on_invalid_utf8(
    pilot: ModuleType,
    tmp_path: Path,
) -> None:
    owner = next(owner for owner in pilot.OWNERS if owner.key == "update-plan")
    _meta_path(pilot, tmp_path, owner).write_bytes(b"\xff")

    with pytest.raises(pilot.PilotError, match="invalid mutmut metadata"):
        pilot._load_exit_codes(owner, tmp_path)


def test_load_exit_codes_raises_on_missing_exit_code_map(pilot: ModuleType, tmp_path: Path) -> None:
    owner = next(owner for owner in pilot.OWNERS if owner.key == "update-plan")
    _write_meta(pilot, tmp_path, owner, {"not_exit_code_by_key": {}})

    with pytest.raises(pilot.PilotError, match="exit_code_by_key missing"):
        pilot._load_exit_codes(owner, tmp_path)


@pytest.mark.parametrize("exit_code", ["0", True])
def test_load_exit_codes_raises_on_wrong_type_exit_code_value(
    pilot: ModuleType,
    tmp_path: Path,
    exit_code: object,
) -> None:
    owner = next(owner for owner in pilot.OWNERS if owner.key == "update-plan")
    raw_name = f"{owner.module}.x_build_update_plan__mutmut_2"
    _write_meta(pilot, tmp_path, owner, {"exit_code_by_key": {raw_name: exit_code}})

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
    owner = next(owner for owner in pilot.OWNERS if owner.key == "update-plan")
    # The trailing owner wildcard matches this key before canonicalization rejects it.
    raw_name = f"{owner.module}.x_build_update_plan__mutmut_2.stray"
    _write_meta(pilot, tmp_path, owner, {"exit_code_by_key": {raw_name: 0}})

    with pytest.raises(pilot.PilotError, match="invalid mutmut name"):
        pilot._load_exit_codes(owner, tmp_path)


def test_load_exit_codes_raises_on_duplicate_canonical_name(
    pilot: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


@pytest.mark.windows_compat
def test_write_report_is_deterministic_atomic_and_printable_ascii(
    pilot: ModuleType, tmp_path: Path
) -> None:
    report_path = tmp_path / "nested" / "report.json"
    payload = {"z": "snowman \u2603", "a": {"z": 2, "a": 1}}

    pilot._write_report(report_path, payload)
    first = report_path.read_bytes()
    pilot._write_report(
        report_path,
        {"a": {"a": 1, "z": 2}, "z": "snowman \u2603"},
    )

    assert report_path.read_bytes() == first
    assert all(byte < 128 for byte in first)
    assert first == (
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    ).encode("ascii")
    assert not (report_path.parent / f".{report_path.name}.tmp").exists()


@pytest.mark.windows_compat
def test_write_report_preserves_existing_file_when_atomic_replace_fails(
    pilot: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text("previous\n", encoding="ascii")

    def fail_replace(source: str, target: str) -> None:
        raise OSError("replace failed")

    # _write_report routes through the canonical apm_cli.utils.atomic_io
    # atomic-write primitive, which performs its rename via os.replace
    # (not Path.replace) -- patch the real call site.
    monkeypatch.setattr("apm_cli.utils.atomic_io.os.replace", fail_replace)

    with pytest.raises(pilot.PilotError, match="failed to write mutation report"):
        pilot._write_report(report_path, {"status": "accepted"})

    assert not (report_path.parent / f".{report_path.name}.tmp").exists()
    assert report_path.read_text(encoding="ascii") == "previous\n"
