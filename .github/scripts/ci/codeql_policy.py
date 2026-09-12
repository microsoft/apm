#!/usr/bin/env python3
"""Read-only, fail-closed CodeQL policy executed from the trusted base commit."""

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

WORKFLOW = ".github/workflows/codeql.yml"
ANALYSIS_KEY = f"{WORKFLOW}:analyze"
LANGUAGES = ("python", "actions", "javascript-typescript")
POLL_SECONDS = 15
Json = dict[str, Any]


class PolicyError(RuntimeError):
    """Evidence is invalid, inaccessible, or explicitly unsuccessful."""


class Pending(PolicyError):
    """Evidence has not finished arriving; retry within the gate's deadline."""


@dataclass(frozen=True)
class Context:
    """Immutable event identity; PR analyses belong to the merge, not head, SHA."""

    repository: str
    event: str
    sha: str
    ref: str
    head_sha: str

    def endpoint(self, resource: str, **query: str) -> str:
        """Build repository-scoped, read-only API paths."""
        return f"repos/{self.repository}/{resource}?{urlencode(query | {'per_page': '100'})}"


def object_value(value: Any, label: str) -> Json:
    """Reject malformed API/event objects instead of treating them as empty."""
    if not isinstance(value, dict):
        raise PolicyError(f"Invalid {label}: expected an object")
    return value


def commit_sha(value: Any) -> str:
    """Accept only complete immutable Git commit IDs."""
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise PolicyError("Invalid commit SHA in GitHub event")
    return value


def positive_id(value: Any, label: str) -> int:
    """Validate IDs and counts before using them in API paths or comparisons."""
    if type(value) is not int or value <= 0:
        raise PolicyError(f"Invalid {label}: expected a positive integer")
    return value


def load_context(environ: Mapping[str, str]) -> Context:
    """Read runner-owned event metadata, never a PR-provided policy or artifact."""
    try:
        event = object_value(
            json.loads(Path(environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8")),
            "event",
        )
        repo = environ["GITHUB_REPOSITORY"]
        name = environ["GITHUB_EVENT_NAME"]
        sha = commit_sha(environ["GITHUB_SHA"])
        ref = environ["GITHUB_REF"]
        repository = object_value(event["repository"], "repository")
        if (
            re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) is None
            or repository["full_name"] != repo
            or repository["default_branch"] != "main"
        ):
            raise PolicyError("Policy requires the event repository's main branch")
        if name == "pull_request":
            pr = object_value(event["pull_request"], "pull request")
            number = positive_id(event["number"], "PR number")
            if (
                ref != f"refs/pull/{number}/merge"
                or pr["merge_commit_sha"] != sha
                or pr["base"]["ref"] != "main"
                or pr["base"]["repo"]["full_name"] != repo
            ):
                raise PolicyError("PR merge ref, SHA, or base repository does not match the event")
            head_sha = commit_sha(pr["head"]["sha"])
        elif name == "merge_group":
            group = object_value(event["merge_group"], "merge group")
            if (
                group["head_sha"] != sha
                or group["head_ref"] != ref
                or not ref.startswith("refs/heads/gh-readonly-queue/main/")
                or group["base_ref"] != "refs/heads/main"
            ):
                raise PolicyError("Merge-queue ref, SHA, or base does not match the event")
            head_sha = sha
        else:
            raise PolicyError(f"Unsupported CodeQL policy event: {name}")
    except (KeyError, TypeError, OSError, ValueError) as exc:
        raise PolicyError("Missing or malformed GitHub event metadata") from exc
    return Context(repo, name, sha, ref, head_sha)


class GitHub:
    """Paginated GET-only transport using the runner's existing gh authentication."""

    def request(self, endpoint: str, *, paginated: bool) -> bytes:
        """Make a bounded GET request without exposing authentication diagnostics."""
        executable = shutil.which("gh")
        if executable is None:
            raise PolicyError("gh CLI is required for the CodeQL policy")
        try:
            command = [
                executable,
                "api",
                "--hostname",
                "github.com",
                "--method",
                "GET",
                "-H",
                "Accept: application/vnd.github+json",
                "-H",
                "X-GitHub-Api-Version: 2022-11-28",
                endpoint,
            ]
            if paginated:
                command.extend(["--paginate", "--slurp"])
            result = subprocess.run(  # noqa: S603 -- fixed executable, argv-only GET requests
                command,
                capture_output=True,
                check=False,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PolicyError("GitHub API request could not complete; rerun the gate") from exc
        if result.returncode:
            raise PolicyError(
                "GitHub API request failed; verify actions:read and security-events:read "
                f"permissions and rerun the gate ({endpoint.split('?')[0]})"
            )
        return result.stdout

    def pages(self, endpoint: str, key: str | None = None) -> list[Json]:
        """Read every response page; permission/transport/JSON failures are fatal."""
        try:
            pages = json.loads(self.request(endpoint, paginated=True))
            if not isinstance(pages, list):
                raise PolicyError("Invalid GitHub API pagination response")
            records: list[Json] = []
            for page in pages:
                items = object_value(page, "API page").get(key) if key else page
                if not isinstance(items, list):
                    raise PolicyError("Invalid GitHub API collection response")
                records.extend(object_value(item, "API record") for item in items)
            return records
        except (ValueError, TypeError) as exc:
            raise PolicyError("Invalid JSON from GitHub API") from exc

    def artifact(self, context: Context, identifier: int, language: str) -> Json:
        """Read one small JSON entry in memory; never extract or execute artifacts."""
        payload = self.request(
            f"repos/{context.repository}/actions/artifacts/{identifier}/zip", paginated=False
        )
        if len(payload) > 65536:
            raise PolicyError("CodeQL evidence artifact is too large")
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                entries = archive.infolist()
                if (
                    len(entries) != 1
                    or entries[0].filename != f"codeql-policy-{language}.json"
                    or entries[0].file_size > 16384
                ):
                    raise PolicyError("Unexpected CodeQL evidence artifact contents")
                return object_value(json.loads(archive.read(entries[0])), "CodeQL evidence")
        except (zipfile.BadZipFile, OSError, RuntimeError, ValueError) as exc:
            raise PolicyError("Unreadable CodeQL evidence artifact; rerun CodeQL") from exc


def successful_run(context: Context, api: GitHub) -> tuple[int, int]:
    """Select the newest matching workflow run, including its current rerun attempt."""
    runs = api.pages(
        context.endpoint(
            f"actions/workflows/{WORKFLOW.rsplit('/', 1)[-1]}/runs",
            event=context.event,
            head_sha=context.head_sha,
        ),
        "workflow_runs",
    )
    matching = [
        run
        for run in runs
        if run.get("path") == WORKFLOW
        and run.get("event") == context.event
        and run.get("head_sha") == context.head_sha
    ]
    if not matching:
        raise Pending("CodeQL workflow has not started for this event")
    run = max(matching, key=lambda item: positive_id(item.get("id"), "workflow run ID"))
    if run.get("status") != "completed":
        raise Pending("CodeQL workflow is still running")
    if run.get("conclusion") != "success":
        raise PolicyError(f"CodeQL workflow did not succeed: {run.get('conclusion')}")
    return positive_id(run["id"], "workflow run ID"), positive_id(
        run.get("run_attempt"), "workflow run attempt"
    )


def successful_jobs(context: Context, api: GitHub, run_id: int) -> None:
    """A skipped, neutral, removed, or failing language job cannot satisfy policy."""
    jobs = api.pages(context.endpoint(f"actions/runs/{run_id}/jobs", filter="latest"), "jobs")
    for language in LANGUAGES:
        name = f"Analyze ({language})"
        matching = [job for job in jobs if job.get("name") == name]
        if not matching:
            raise Pending(f"Required CodeQL job is missing: {name}")
        if len(matching) != 1:
            raise PolicyError(f"Ambiguous CodeQL jobs: {name}")
        job = matching[0]
        if job.get("status") != "completed":
            raise Pending(f"Required CodeQL job is still running: {name}")
        if job.get("conclusion") != "success":
            raise PolicyError(f"{name} did not succeed: {job.get('conclusion')}")


def configuration_language(record: Json) -> str | None:
    """Match the workflow's full analysis identity, not merely the CodeQL tool name."""
    if record.get("analysis_key") != ANALYSIS_KEY:
        return None
    for language in LANGUAGES:
        if record.get("category") == f"{ANALYSIS_KEY}/language:{language}":
            try:
                environment = json.loads(record.get("environment", ""))
            except (ValueError, TypeError) as exc:
                raise PolicyError(f"Malformed {language} analysis environment") from exc
            if environment != {"language": language}:
                raise PolicyError(f"Mismatched {language} analysis environment")
            return language
    return None


def run_evidence(context: Context, api: GitHub, run: tuple[int, int]) -> dict[str, str]:
    """Bind every SARIF upload to its exact workflow run, attempt, language, and commit."""
    artifacts = api.pages(context.endpoint(f"actions/runs/{run[0]}/artifacts"), "artifacts")
    uploads: dict[str, str] = {}
    for language in LANGUAGES:
        name = f"codeql-policy-{language}-{run[1]}"
        matching = [item for item in artifacts if item.get("name") == name]
        if not matching:
            raise Pending(f"Missing current-attempt CodeQL evidence: {language}")
        if len(matching) != 1 or matching[0].get("expired") is not False:
            raise PolicyError(f"Ambiguous or expired CodeQL evidence: {language}; rerun CodeQL")
        identifier = positive_id(matching[0].get("id"), "artifact ID")
        evidence = api.artifact(context, identifier, language)
        expected = {
            "run_id": str(run[0]),
            "run_attempt": str(run[1]),
            "language": language,
            "sha": context.sha,
            "ref": context.ref,
        }
        if any(evidence.get(key) != value for key, value in expected.items()):
            raise PolicyError(f"CodeQL evidence does not match this execution: {language}")
        sarif_id = evidence.get("sarif_id")
        if (
            not isinstance(sarif_id, str)
            or re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", sarif_id) is None
        ):
            raise PolicyError(f"Missing or invalid SARIF upload ID: {language}")
        uploads[language] = sarif_id
    return uploads


def successful_analyses(context: Context, api: GitHub, uploads: dict[str, str]) -> tuple[int, ...]:
    """Require processed, nonempty analyses of every language on the exact merge SHA."""
    analyses = api.pages(
        context.endpoint("code-scanning/analyses", ref=context.ref, tool_name="CodeQL")
    )
    latest: dict[str, Json] = {}
    for record in analyses:
        if (
            record.get("ref") != context.ref
            or object_value(record.get("tool"), "analysis tool").get("name") != "CodeQL"
        ):
            continue
        language = configuration_language(record)
        if language is None:
            continue
        identifier = positive_id(record.get("id"), "analysis ID")
        if language not in latest or identifier > latest[language]["id"]:
            latest[language] = record
    missing = set(LANGUAGES) - latest.keys()
    if missing:
        raise Pending(f"Missing processed CodeQL analyses: {', '.join(sorted(missing))}")
    for language, record in latest.items():
        # Alerts describe the moving ref, not a historical analysis snapshot.
        if record.get("commit_sha") != context.sha:
            raise Pending(f"Newest CodeQL analysis is for a different commit: {language}")
        if record.get("sarif_id") != uploads[language]:
            raise Pending(f"Waiting for the current-attempt SARIF upload: {language}")
        if record.get("error") != "":
            raise PolicyError(f"CodeQL analysis has an error for {language}")
        positive_id(record.get("rules_count"), f"{language} analyzed rule count")
    return tuple(latest[language]["id"] for language in LANGUAGES)


def above_threshold(alert: Json) -> bool:
    """Preserve the ruleset's high-or-higher security and error alert thresholds."""
    rule = object_value(alert.get("rule"), "alert rule")
    if "security_severity_level" not in rule or "severity" not in rule:
        raise PolicyError("Missing CodeQL alert severity")
    security = rule["security_severity_level"]
    severity = rule["severity"]
    if security not in (None, "low", "medium", "high", "critical") or severity not in (
        "none",
        "note",
        "warning",
        "error",
    ):
        raise PolicyError("Unknown CodeQL alert severity")
    return security in ("high", "critical") or severity == "error"


def open_findings(context: Context, api: GitHub) -> list[int]:
    """Block all open scoped findings, including pre-existing ones, honoring dismissals."""
    alerts = api.pages(
        context.endpoint("code-scanning/alerts", ref=context.ref, tool_name="CodeQL", state="open")
    )
    blocked: set[int] = set()
    for alert in alerts:
        if alert.get("state") == "dismissed":
            continue
        if alert.get("state") not in ("open", "fixed"):
            raise PolicyError("Unknown CodeQL alert state")
        if not above_threshold(alert):
            continue
        number = positive_id(alert.get("number"), "alert number")
        instances = api.pages(
            context.endpoint(f"code-scanning/alerts/{number}/instances", ref=context.ref)
        )
        if not instances:
            raise Pending(f"Missing instances for CodeQL alert {number}")
        for instance in instances:
            if instance.get("ref") != context.ref:
                raise PolicyError(f"Unexpected ref in CodeQL alert {number} response")
            if instance.get("analysis_key") != ANALYSIS_KEY:
                continue
            if configuration_language(instance) is None:
                raise PolicyError(f"Unknown workflow configuration for CodeQL alert {number}")
            if instance.get("state") == "fixed":
                continue
            if instance.get("state") != "open":
                raise PolicyError(f"Unknown instance state for CodeQL alert {number}")
            if instance.get("commit_sha") != context.sha:
                raise Pending(f"Stale instance for CodeQL alert {number}")
            blocked.add(number)
    return sorted(blocked)


def evaluate(context: Context, api: GitHub) -> list[int]:
    """Evaluate a stable completed snapshot; concurrent reruns cannot reuse a pass."""
    run = successful_run(context, api)
    successful_jobs(context, api, run[0])
    uploads = run_evidence(context, api, run)
    analyses = successful_analyses(context, api, uploads)
    blocked = open_findings(context, api)
    if (
        successful_run(context, api) != run
        or successful_analyses(context, api, uploads) != analyses
    ):
        raise Pending("CodeQL evidence changed during evaluation")
    return blocked


def main() -> int:
    """Run within the enclosing gate deadline without importing any project code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deadline", type=int, required=True, help="Gate deadline as Unix seconds")
    args = parser.parse_args()
    try:
        context = load_context(os.environ)
        api = GitHub()
        last_pending = "CodeQL policy has not run"
        while time.time() < args.deadline:
            try:
                blocked = evaluate(context, api)
            except Pending as exc:
                last_pending = str(exc)
                sys.stderr.write(f"[codeql-policy] {last_pending}\n")
                time.sleep(min(POLL_SECONDS, max(0, args.deadline - time.time())))
                continue
            if time.time() >= args.deadline:
                raise PolicyError("CodeQL policy deadline expired; rerun gate")
            if blocked:
                links = [
                    f"https://github.com/{context.repository}/security/code-scanning/{number}"
                    for number in blocked
                ]
                raise PolicyError(
                    "Open high/critical or error-level CodeQL findings block merge. "
                    "Fix or review/dismiss the findings, then rerun gate: " + ", ".join(links)
                )
            sys.stdout.write(
                f"[codeql-policy] Passed for {context.sha}: all language jobs and analyses "
                "succeeded; no open workflow-scoped findings at blocking severity.\n"
            )
            return 0
        raise PolicyError(
            f"Timed out: {last_pending}. Rerun CodeQL and gate; re-enqueue merge-queue entries."
        )
    except PolicyError as exc:
        # GitHub workflow-command data must not interpret newlines or percent escapes.
        message = str(exc).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        sys.stderr.write(f"::error title=CodeQL policy failed::{message}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
