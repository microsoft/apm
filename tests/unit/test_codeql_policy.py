"""Behavioral contracts for the trusted, read-only CodeQL merge policy."""

import importlib.util
import io
import json
import os
import subprocess
import sys
import zipfile
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from tests.workflow_contracts import load_workflow, workflow_job

pytestmark = pytest.mark.component
ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github/scripts/ci/codeql_policy.py"
SPEC = importlib.util.spec_from_file_location("codeql_policy", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
policy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = policy
SPEC.loader.exec_module(policy)

MERGE_SHA = "a" * 40
HEAD_SHA = "b" * 40
BASE_SHA = "c" * 40
REF = "refs/pull/42/merge"
REPO = "microsoft/apm"


@pytest.fixture
def context() -> Any:
    return policy.Context(REPO, "pull_request", MERGE_SHA, REF, HEAD_SHA)


def analysis(language: str, **overrides: Any) -> dict[str, Any]:
    return {
        "id": 100 + policy.LANGUAGES.index(language),
        "ref": REF,
        "commit_sha": MERGE_SHA,
        "analysis_key": policy.ANALYSIS_KEY,
        "category": f"{policy.ANALYSIS_KEY}/language:{language}",
        "environment": json.dumps({"language": language}),
        "tool": {"name": "CodeQL"},
        "error": "",
        "rules_count": 17,
        "sarif_id": f"00000000-0000-0000-0000-{policy.LANGUAGES.index(language):012d}",
        **overrides,
    }


def instance(language: str = "python", **overrides: Any) -> dict[str, Any]:
    record = analysis(language)
    return {
        key: record[key] for key in ("ref", "commit_sha", "analysis_key", "category", "environment")
    } | {"state": "open", **overrides}


def alert(
    number: int = 7, security: Any = "high", level: str = "warning", **overrides: Any
) -> dict[str, Any]:
    return {
        "number": number,
        "state": "open",
        "rule": {"security_severity_level": security, "severity": level},
        **overrides,
    }


class FakeGitHub:
    def __init__(self) -> None:
        self.run = {
            "id": 12,
            "run_attempt": 1,
            "path": policy.WORKFLOW,
            "event": "pull_request",
            "head_sha": HEAD_SHA,
            "status": "completed",
            "conclusion": "success",
        }
        self.jobs = [
            {"name": f"Analyze ({language})", "status": "completed", "conclusion": "success"}
            for language in policy.LANGUAGES
        ]
        self.analyses = [analysis(language) for language in policy.LANGUAGES]
        self.alerts = []
        self.instances = {}
        self.calls = []
        self.artifacts = [
            {"id": index + 1, "name": f"codeql-policy-{language}-1", "expired": False}
            for index, language in enumerate(policy.LANGUAGES)
        ]
        self.proofs = {
            language: {
                "run_id": "12",
                "run_attempt": "1",
                "sha": MERGE_SHA,
                "ref": REF,
                "language": language,
                "sarif_id": analysis(language)["sarif_id"],
            }
            for language in policy.LANGUAGES
        }

    def artifact(self, context: Any, identifier: int, language: str) -> dict[str, Any]:
        return deepcopy(self.proofs[language])

    def pages(self, path: str, key: str | None = None) -> list[dict[str, Any]]:
        self.calls.append((path, key))
        parsed = urlparse(path)
        query = parse_qs(parsed.query)
        if parsed.path.endswith("/runs"):
            assert query["head_sha"] == [HEAD_SHA]
            return [deepcopy(self.run)]
        if parsed.path.endswith("/jobs"):
            assert query["filter"] == ["latest"]
            return deepcopy(self.jobs)
        if parsed.path.endswith("/artifacts"):
            return deepcopy(self.artifacts)
        if parsed.path.endswith("/analyses"):
            assert query["ref"] == [REF]
            return deepcopy(self.analyses)
        if parsed.path.endswith("/alerts"):
            assert query["ref"] == [REF]
            assert query["state"] == ["open"]
            return deepcopy(self.alerts)
        if parsed.path.endswith("/instances"):
            assert query["ref"] == [REF]
            return deepcopy(self.instances[int(parsed.path.split("/")[-2])])
        raise AssertionError(f"Unexpected endpoint: {path}")


def test_clean_exact_commit_with_no_sdl_pr_upload_passes(context: Any) -> None:
    api = FakeGitHub()
    assert policy.evaluate(context, api) == []
    assert any(urlparse(path).path.endswith("/alerts") for path, _ in api.calls)


def test_policy_languages_match_the_workflow_matrix() -> None:
    workflow = load_workflow(ROOT / ".github/workflows/codeql.yml")
    assert workflow_job(workflow, "analyze")["strategy"]["matrix"]["language"] == list(
        policy.LANGUAGES
    )


@pytest.mark.parametrize("language", policy.LANGUAGES)
def test_each_language_is_mandatory(context: Any, language: str) -> None:
    api = FakeGitHub()
    api.analyses = [record for record in api.analyses if record != analysis(language)]
    with pytest.raises(policy.Pending, match=language):
        policy.evaluate(context, api)


@pytest.mark.parametrize(
    "overrides",
    [
        {"commit_sha": HEAD_SHA},
        {"ref": "refs/heads/main"},
        {"analysis_key": "(default)", "category": ""},
        {"category": "/language:python"},
        {"environment": '{"language":"actions"}'},
        {"tool": {"name": "Another scanner"}},
    ],
)
def test_wrong_analysis_cannot_supply_required_python_result(
    context: Any, overrides: dict[str, Any]
) -> None:
    api = FakeGitHub()
    api.analyses[0] = analysis("python", **overrides)
    with pytest.raises(policy.PolicyError, match="python"):
        policy.evaluate(context, api)


@pytest.mark.parametrize("overrides", [{"error": "extraction failed"}, {"rules_count": 0}])
def test_failed_or_empty_analysis_fails_closed(context: Any, overrides: dict[str, Any]) -> None:
    api = FakeGitHub()
    api.analyses[0] = analysis("python", **overrides)
    with pytest.raises(policy.PolicyError, match="python"):
        policy.evaluate(context, api)


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "skipped", "neutral"])
def test_workflow_must_succeed_not_merely_finish(context: Any, conclusion: str) -> None:
    api = FakeGitHub()
    api.run["conclusion"] = conclusion
    with pytest.raises(policy.PolicyError, match="workflow"):
        policy.evaluate(context, api)


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "skipped", "neutral"])
def test_each_language_job_must_really_run(context: Any, conclusion: str) -> None:
    api = FakeGitHub()
    api.jobs[0]["conclusion"] = conclusion
    with pytest.raises(policy.PolicyError, match="Analyze"):
        policy.evaluate(context, api)


def test_in_progress_and_missing_jobs_wait(context: Any) -> None:
    api = FakeGitHub()
    api.jobs.pop()
    with pytest.raises(policy.Pending, match="javascript-typescript"):
        policy.evaluate(context, api)
    api.run["status"] = "in_progress"
    with pytest.raises(policy.Pending, match="workflow"):
        policy.evaluate(context, api)


@pytest.mark.parametrize(
    ("security", "level"),
    [("high", "warning"), ("critical", "note"), (None, "error"), ("low", "error")],
)
def test_thresholds_block_all_open_findings_including_existing(
    context: Any, security: str | None, level: str
) -> None:
    api = FakeGitHub()
    api.alerts = [alert(security=security, level=level, created_at="2020-01-01T00:00:00Z")]
    api.instances[7] = [instance()]
    assert policy.evaluate(context, api) == [7]


@pytest.mark.parametrize(
    ("security", "level"),
    [("medium", "warning"), ("low", "note"), (None, "warning"), (None, "none")],
)
def test_below_threshold_findings_do_not_block(
    context: Any, security: str | None, level: str
) -> None:
    api = FakeGitHub()
    api.alerts = [alert(security=security, level=level)]
    assert policy.evaluate(context, api) == []


def test_dismissed_alert_remains_dismissed(context: Any) -> None:
    api = FakeGitHub()
    api.alerts = [alert(state="dismissed")]
    assert policy.evaluate(context, api) == []


def test_sdl_and_old_categories_are_not_premerge_authorities(context: Any) -> None:
    api = FakeGitHub()
    api.alerts = [alert()]
    api.instances[7] = [
        instance(analysis_key="(default)", category="", environment="{}"),
        instance(analysis_key="/language:python", category="/language:python", environment="{}"),
    ]
    assert policy.evaluate(context, api) == []


def test_all_instances_are_inspected_not_only_most_recent(context: Any) -> None:
    api = FakeGitHub()
    api.alerts = [alert(most_recent_instance=instance(analysis_key="(default)", category=""))]
    api.instances[7] = [
        instance(analysis_key="(default)", category=""),
        instance(),
    ]
    assert policy.evaluate(context, api) == [7]


def test_branch_instance_not_default_branch_state_determines_blocking(context: Any) -> None:
    api = FakeGitHub()
    api.alerts = [alert(state="fixed")]
    api.instances[7] = [instance()]
    assert policy.evaluate(context, api) == [7]


def test_fixed_instance_does_not_block(context: Any) -> None:
    api = FakeGitHub()
    api.alerts = [alert()]
    api.instances[7] = [instance(state="fixed", commit_sha=BASE_SHA)]
    assert policy.evaluate(context, api) == []


@pytest.mark.parametrize("records", [[], [instance(commit_sha=BASE_SHA)]])
def test_missing_or_stale_alert_instances_never_pass(
    context: Any, records: list[dict[str, Any]]
) -> None:
    api = FakeGitHub()
    api.alerts = [alert()]
    api.instances[7] = records
    with pytest.raises(policy.Pending, match="alert"):
        policy.evaluate(context, api)


@pytest.mark.parametrize("bad_value", ["urgent", "", 7])
def test_unknown_security_severity_fails_closed(context: Any, bad_value: Any) -> None:
    api = FakeGitHub()
    api.alerts = [alert(security=bad_value)]
    with pytest.raises(policy.PolicyError, match="severity"):
        policy.evaluate(context, api)


def test_newer_failed_analysis_does_not_fall_back_to_old_success(context: Any) -> None:
    api = FakeGitHub()
    api.analyses.insert(0, analysis("python", id=999, error="failure"))
    with pytest.raises(policy.PolicyError, match="python"):
        policy.evaluate(context, api)


def test_same_commit_rerun_race_waits(context: Any) -> None:
    class RerunGitHub(FakeGitHub):
        def pages(self, path: str, key: str | None = None) -> list[dict[str, Any]]:
            records = super().pages(path, key)
            if urlparse(path).path.endswith("/alerts"):
                self.run["run_attempt"] += 1
            return records

    with pytest.raises(policy.Pending, match="changed"):
        policy.evaluate(context, RerunGitHub())


def test_api_failure_is_not_treated_as_no_alerts(context: Any) -> None:
    class BrokenGitHub(FakeGitHub):
        def pages(self, path: str, key: str | None = None) -> list[dict[str, Any]]:
            if urlparse(path).path.endswith("/alerts"):
                raise policy.PolicyError("GitHub API unavailable")
            return super().pages(path, key)

    with pytest.raises(policy.PolicyError, match="API unavailable"):
        policy.evaluate(context, BrokenGitHub())


@pytest.fixture
def event_env(tmp_path: Path) -> dict[str, str]:
    event = {
        "number": 42,
        "repository": {"full_name": REPO, "default_branch": "main"},
        "pull_request": {
            "merge_commit_sha": MERGE_SHA,
            "head": {"sha": HEAD_SHA},
            "base": {"ref": "main", "sha": BASE_SHA, "repo": {"full_name": REPO}},
        },
    }
    path = tmp_path / "event.json"
    path.write_text(json.dumps(event), encoding="utf-8")
    return {
        "GITHUB_EVENT_PATH": str(path),
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_SHA": MERGE_SHA,
        "GITHUB_REF": REF,
    }


def test_pull_request_context_uses_merge_commit_not_head(
    event_env: dict[str, str], context: Any
) -> None:
    assert policy.load_context(event_env) == context


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("GITHUB_EVENT_NAME", "pull_request_target"),
        ("GITHUB_SHA", HEAD_SHA),
        ("GITHUB_REF", "refs/heads/main"),
        ("GITHUB_REPOSITORY", "attacker/repo"),
    ],
)
def test_invalid_event_context_is_rejected(event_env: dict[str, str], key: str, value: str) -> None:
    event_env[key] = value
    with pytest.raises(policy.PolicyError):
        policy.load_context(event_env)


def test_queue_context_uses_its_own_commit(event_env: dict[str, str]) -> None:
    ref = f"refs/heads/gh-readonly-queue/main/pr-42-{HEAD_SHA}"
    event = {
        "repository": {"full_name": REPO, "default_branch": "main"},
        "merge_group": {
            "head_sha": MERGE_SHA,
            "head_ref": ref,
            "base_sha": BASE_SHA,
            "base_ref": "refs/heads/main",
        },
    }
    Path(event_env["GITHUB_EVENT_PATH"]).write_text(json.dumps(event), encoding="utf-8")
    event_env.update(GITHUB_EVENT_NAME="merge_group", GITHUB_REF=ref)
    assert policy.load_context(event_env) == policy.Context(
        REPO, "merge_group", MERGE_SHA, ref, MERGE_SHA
    )


def test_api_pagination_collects_later_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, json.dumps([[{"id": 1}], [{"id": 2}]]), "")

    monkeypatch.setattr(policy.subprocess, "run", run)
    assert policy.GitHub().pages("repos/microsoft/apm/code-scanning/alerts?per_page=100") == [
        {"id": 1},
        {"id": 2},
    ]
    command, kwargs = calls[0]
    assert "--paginate" in command
    assert "--slurp" in command
    assert command[command.index("--method") + 1] == "GET"
    assert kwargs["timeout"] > 0


@pytest.mark.parametrize("payload", ["not json", '{"message":"denied"}', '[{"wrong":[]}]'])
def test_malformed_api_response_fails_closed(monkeypatch: pytest.MonkeyPatch, payload: str) -> None:
    monkeypatch.setattr(
        policy.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, payload, ""),
    )
    with pytest.raises(policy.PolicyError):
        policy.GitHub().pages("repos/microsoft/apm/actions/runs?per_page=100", "workflow_runs")


def test_http_failure_is_explicit_and_does_not_leak_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        policy.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "", "secret-token"),
    )
    with pytest.raises(policy.PolicyError, match="GitHub API") as failure:
        policy.GitHub().pages("repos/microsoft/apm/code-scanning/alerts")
    assert "secret-token" not in str(failure.value)


def test_newer_commit_analysis_cannot_use_historical_alert_snapshot(context: Any) -> None:
    api = FakeGitHub()
    api.analyses.insert(0, analysis("python", id=999, commit_sha=HEAD_SHA))
    with pytest.raises(policy.Pending, match="different commit"):
        policy.evaluate(context, api)


def test_completed_rerun_cannot_reuse_previous_attempt_sarif(context: Any) -> None:
    api = FakeGitHub()
    api.run["run_attempt"] = 2
    for artifact in api.artifacts:
        artifact["name"] = artifact["name"][:-1] + "2"
    for proof in api.proofs.values():
        proof["run_attempt"] = "2"
        proof["sarif_id"] = "11111111-1111-1111-1111-111111111111"
    with pytest.raises(policy.Pending, match="current-attempt"):
        policy.evaluate(context, api)
    for record in api.analyses:
        record["sarif_id"] = "11111111-1111-1111-1111-111111111111"
    assert policy.evaluate(context, api) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", "99"),
        ("run_attempt", "2"),
        ("sha", HEAD_SHA),
        ("language", "ruby"),
        ("ref", "refs/heads/main"),
        ("sarif_id", ""),
    ],
)
def test_artifact_cannot_lie_about_its_execution(context: Any, field: str, value: str) -> None:
    api = FakeGitHub()
    api.proofs["python"][field] = value
    with pytest.raises(policy.PolicyError):
        policy.evaluate(context, api)


def test_expired_or_missing_artifacts_fail_closed(context: Any) -> None:
    api = FakeGitHub()
    api.artifacts[0]["expired"] = True
    with pytest.raises(policy.PolicyError, match="expired"):
        policy.evaluate(context, api)
    api.artifacts.pop(0)
    with pytest.raises(policy.Pending, match="evidence"):
        policy.evaluate(context, api)


@pytest.mark.parametrize("filename", ["../outside.json", "wrong.json"])
def test_artifact_reader_rejects_unexpected_names(
    context: Any, monkeypatch: pytest.MonkeyPatch, filename: str
) -> None:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(filename, "{}")
    api = policy.GitHub()
    monkeypatch.setattr(api, "request", lambda *args, **kwargs: payload.getvalue())
    with pytest.raises(policy.PolicyError):
        api.artifact(context, 1, "python")


def test_artifact_reader_accepts_bounded_json(
    context: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    proof = FakeGitHub().proofs["python"]
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("codeql-policy-python.json", json.dumps(proof))
    api = policy.GitHub()
    monkeypatch.setattr(api, "request", lambda *args, **kwargs: payload.getvalue())
    assert api.artifact(context, 1, "python") == proof


@pytest.mark.skipif(os.name != "posix", reason="Exercises the Ubuntu merge-gate Bash runner")
def test_shell_rechecks_codeql_after_other_checks_finish(tmp_path: Path) -> None:
    """An initial CodeQL pass must not survive a later failing rerun."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    counter = tmp_path / "calls"
    helpers = {
        "python3": f"""#!/bin/sh
if [ -f "{counter}" ]; then
  echo "CodeQL rerun failed" >&2
  exit 1
fi
echo first > "{counter}"
""",
        "gh": '#!/bin/sh\necho \'{"check_runs":[{"status":"completed",'
        '"conclusion":"success","started_at":"2026-09-12T00:00:00Z","html_url":""}]}\'\n',
        "sleep": "#!/bin/sh\nexit 0\n",
    }
    for name, content in helpers.items():
        path = bin_dir / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)
    environment = os.environ | {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GH_TOKEN": "test-token",
        "REPO": REPO,
        "SHA": HEAD_SHA,
        "EXPECTED_CHECKS": "CI",
        "TIMEOUT_MIN": "1",
    }
    result = subprocess.run(
        ["bash", str(ROOT / ".github/scripts/ci/merge_gate_wait.sh")],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 1
    assert "CodeQL rerun failed" in result.stderr
    assert "completed successfully" not in result.stdout
