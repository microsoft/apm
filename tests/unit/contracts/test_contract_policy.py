"""Read-only no-policy acquisition never mistakes unknown for ungoverned."""

import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from apm_cli.contracts.frontend import plan_contract
from apm_cli.contracts.models import ContractError, Outcome
from apm_cli.policy import contract_prerequisite, discovery
from apm_cli.policy.discovery import PolicyFetchResult, discover_contract_policy

pytestmark = pytest.mark.component


@pytest.fixture(autouse=True)
def no_policy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APM_POLICY_DISABLE", raising=False)
    monkeypatch.delenv("APM_NO_SCRIPTS", raising=False)


def test_no_repository_is_positive_no_remote_without_process_or_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbidden = Mock(side_effect=AssertionError("acquisition side effect"))
    monkeypatch.setattr(discovery, "discover_policy_with_chain", forbidden)
    monkeypatch.setattr(discovery, "discover_policy", forbidden)
    monkeypatch.setattr(discovery, "_write_cache", forbidden)
    monkeypatch.setattr(contract_prerequisite.subprocess, "run", forbidden)
    result = discover_contract_policy(tmp_path, manifest_data={"name": "fixture"})
    assert result.outcome == "no_git_remote"
    forbidden.assert_not_called()


@pytest.mark.parametrize(
    ("stdout", "returncode", "expected"),
    [
        ("", 0, "no_git_remote"),
        ("origin\n", 0, "cache_miss_fetch_fail"),
        ("upstream\n", 0, "cache_miss_fetch_fail"),
        ("", 128, "cache_miss_fetch_fail"),
    ],
)
def test_all_remotes_and_git_errors_stay_distinct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: str, returncode: int, expected: str
) -> None:
    (tmp_path / ".git").mkdir()
    runner = Mock(return_value=subprocess.CompletedProcess([], returncode, stdout, ""))
    monkeypatch.setattr(contract_prerequisite.subprocess, "run", runner)
    result = discover_contract_policy(tmp_path, manifest_data={"name": "fixture"})
    assert result.outcome == expected
    assert runner.call_args.args[0][-1] == "remote"
    assert runner.call_args.kwargs["timeout"] == 5


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        ({"fetch_failure_default": "block"}, "cache_miss_fetch_fail"),
        ({}, "cache_miss_fetch_fail"),
        ({"hash": "sha256:" + "0" * 64}, "hash_mismatch"),
        ({"hash": "wrong"}, "hash_mismatch"),
        ("org", "hash_mismatch"),
    ],
)
def test_project_requirement_never_becomes_no_policy(
    tmp_path: Path, policy: object, expected: str
) -> None:
    assert discover_contract_policy(tmp_path, manifest_data={"policy": policy}).outcome == expected


def test_disabled_is_not_no_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APM_POLICY_DISABLE", "1")
    assert discover_contract_policy(tmp_path, manifest_data={}).outcome == "disabled"


def test_bare_repository_is_not_assumed_remote_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (tmp_path / "objects").mkdir()
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, "origin\n", ""))
    monkeypatch.setattr(contract_prerequisite.subprocess, "run", runner)
    assert discover_contract_policy(tmp_path, manifest_data={}).outcome == "cache_miss_fetch_fail"
    runner.assert_called_once()


def project_source(tmp_path: Path, policy: str = "") -> Path:
    (tmp_path / "apm.yml").write_text("name: fixture\nversion: 1.0.0\n" + policy, encoding="utf-8")
    path = tmp_path / "work.contract.md"
    path.write_text("---\nproduces: out\nverify: {ok: 'true'}\n---\nWork.\n", encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "outcome", ["absent", "empty", "found", "disabled", "cached_stale", "cache_miss_fetch_fail", ""]
)
def test_plan_refuses_every_nonpositive_policy_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    monkeypatch.setattr(
        discovery, "discover_contract_policy", lambda *a, **k: PolicyFetchResult(outcome=outcome)
    )
    with pytest.raises(ContractError) as error:
        plan_contract(project_source(tmp_path), tmp_path, harness="copilot")
    assert error.value.outcome == Outcome.UNPROVEN
    assert error.value.code == "policy_unavailable"


def test_plan_routes_real_pin_failure_without_legacy_escape(tmp_path: Path) -> None:
    path = project_source(tmp_path, "policy:\n  hash: wrong\n")
    with pytest.raises(ContractError) as error:
        plan_contract(path, tmp_path, harness="copilot")
    assert error.value.outcome == Outcome.UNPROVEN
    assert error.value.code == "policy_blocked"
    assert error.value.__cause__.__class__.__name__ == "PolicyViolationError"


def test_no_scripts_blocks_checks_before_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APM_NO_SCRIPTS", "anything")
    with pytest.raises(ContractError) as error:
        plan_contract(project_source(tmp_path), tmp_path, harness="copilot")
    assert error.value.code == "scripts_disabled"
    assert error.value.outcome == Outcome.UNPROVEN
