#!/usr/bin/env python3
"""Run the bounded mutation-testing pilot and enforce its survivor baseline."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ratchet_baseline import BaselineError, load_baseline, write_baseline

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = REPO_ROOT / "tests" / "mutation" / "baseline.json"
DEFAULT_OUTPUT = REPO_ROOT / "build" / "mutation-pilot" / "report.json"
MUTMUT_VERSION = "3.6.0"
CLASS_NAME_SEPARATOR = chr(0x01C1)
SCHEMA_VERSION = 1

STATUS_BY_EXIT_CODE = {
    None: "not_checked",
    -24: "timeout",
    -11: "segfault",
    -9: "terminated",
    0: "survived",
    1: "killed",
    2: "interrupted",
    3: "killed",
    5: "no_tests",
    24: "timeout",
    33: "no_tests",
    34: "skipped",
    35: "suspicious",
    36: "timeout",
    37: "type_checked",
    152: "timeout",
    255: "timeout",
}
FATAL_STATUSES = frozenset(
    {
        "interrupted",
        "no_tests",
        "not_checked",
        "segfault",
        "skipped",
        "suspicious",
        "terminated",
        "timeout",
        "type_checked",
        "unknown",
    }
)
SENSITIVE_ENVIRONMENT_NAMES = frozenset(
    {
        "ADO_APM_PAT",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
        "ACTIONS_RUNTIME_TOKEN",
        "AZURE_DEVOPS_EXT_PAT",
        "GH_TOKEN",
        "GITHUB_APM_PAT",
        "GITHUB_TOKEN",
        "SSH_AUTH_SOCK",
        "SYSTEM_ACCESSTOKEN",
    }
)


@dataclass(frozen=True)
class Owner:
    """One canonical owner included in the mutation pilot."""

    key: str
    source: str
    functions: tuple[str, ...]
    test_seams: tuple[str, ...]

    @property
    def module(self) -> str:
        """Return the import path corresponding to the source file."""
        return self.source.removeprefix("src/").removesuffix(".py").replace("/", ".")

    @property
    def patterns(self) -> tuple[str, ...]:
        """Return exact mutmut function patterns for this owner."""
        patterns = []
        for function in self.functions:
            class_name, separator, function_name = function.partition(".")
            if separator:
                mangled = (
                    f"x{CLASS_NAME_SEPARATOR}{class_name}{CLASS_NAME_SEPARATOR}{function_name}"
                )
            else:
                mangled = f"x_{function}"
            patterns.append(f"{self.module}.{mangled}*")
        return tuple(patterns)


OWNERS = (
    Owner(
        key="subset-selection",
        source="src/apm_cli/models/dependency/subsets.py",
        functions=(
            "skill_subset_filter_tokens",
            "parse_skill_subset",
            "parse_target_subset",
            "_closest_target",
            "_levenshtein_distance",
        ),
        test_seams=(
            "tests/unit/test_skill_subset_persistence.py",
            "tests/unit/test_dep_targets_persistence.py",
        ),
    ),
    Owner(
        key="update-plan",
        source="src/apm_cli/install/plan.py",
        functions=("build_update_plan",),
        test_seams=("tests/unit/install/test_plan.py",),
    ),
    Owner(
        key="policy-serialization",
        source="src/apm_cli/policy/discovery.py",
        functions=("_policy_to_dict",),
        test_seams=(
            "tests/unit/policy/test_cache_merged_effective.py"
            "::test_policy_serializer_covers_every_dataclass_leaf",
            "tests/unit/policy/test_cache_merged_effective.py"
            "::test_policy_serializer_preserves_sparse_bin_deploy_shapes",
            "tests/unit/policy/test_cache_merged_effective.py"
            "::TestPolicyRoundTrip::test_all_effective_policy_fields_survive_cache_round_trip",
        ),
    ),
    Owner(
        key="link-projection",
        source="src/apm_cli/compilation/link_resolver.py",
        functions=("UnifiedLinkResolver._resolve_in_package_asset_link",),
        test_seams=(
            "tests/unit/compilation/test_link_resolver.py",
            "tests/unit/compilation/test_link_resolver_phase3.py",
            "tests/unit/compilation/test_link_resolver_resolution.py",
        ),
    ),
)


class PilotError(RuntimeError):
    """Raised when mutation evidence cannot be trusted."""


def _ascii(text: str) -> str:
    """Return printable ASCII for terminal-safe diagnostics."""
    return text.encode("ascii", errors="backslashreplace").decode("ascii")


def _canonical_mutant_name(raw_name: str) -> str:
    """Replace mutmut's internal mangling with a stable ASCII name."""
    module, separator, key = raw_name.rpartition(".")
    if not separator:
        raise PilotError(f"invalid mutmut name: {_ascii(raw_name)}")
    if key.startswith("x_"):
        function_mutant = key[2:]
        return f"{module}.{function_mutant}"
    class_prefix = f"x{CLASS_NAME_SEPARATOR}"
    if key.startswith(class_prefix) and CLASS_NAME_SEPARATOR in key[len(class_prefix) :]:
        class_name, function_mutant = key[len(class_prefix) :].split(
            CLASS_NAME_SEPARATOR, maxsplit=1
        )
        return f"{module}.{class_name}.{function_mutant}"
    raise PilotError(f"invalid mutmut name: {_ascii(raw_name)}")


def _load_exit_codes(owner: Owner, repo_root: Path) -> dict[str, int | None]:
    """Load and scope one owner's mutmut metadata."""
    meta_path = repo_root / "mutants" / f"{owner.source}.meta"
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, IsADirectoryError, PermissionError, json.JSONDecodeError) as error:
        raise PilotError(f"invalid mutmut metadata {meta_path}: {error}") from error

    exit_codes = payload.get("exit_code_by_key")
    if not isinstance(exit_codes, dict):
        raise PilotError(f"invalid mutmut metadata {meta_path}: exit_code_by_key missing")

    scoped: dict[str, int | None] = {}
    for raw_name, exit_code in exit_codes.items():
        if not isinstance(raw_name, str):
            raise PilotError(f"invalid mutmut metadata {meta_path}: non-string mutant name")
        if not any(fnmatch.fnmatchcase(raw_name, pattern) for pattern in owner.patterns):
            continue
        if exit_code is not None and not isinstance(exit_code, int):
            raise PilotError(f"invalid mutmut metadata {meta_path}: invalid exit code")
        canonical_name = _canonical_mutant_name(raw_name)
        if canonical_name in scoped:
            raise PilotError(f"duplicate canonical mutant name: {canonical_name}")
        scoped[canonical_name] = exit_code

    if not scoped:
        raise PilotError(f"no mutants matched owner allowlist: {owner.key}")
    return scoped


def _owner_report(owner: Owner, repo_root: Path) -> dict[str, Any]:
    """Build deterministic outcomes for one owner."""
    outcomes: dict[str, list[str]] = {}
    for mutant_name, exit_code in sorted(_load_exit_codes(owner, repo_root).items()):
        status = STATUS_BY_EXIT_CODE.get(exit_code, "unknown")
        outcomes.setdefault(status, []).append(mutant_name)
    counts = {status: len(names) for status, names in sorted(outcomes.items())}
    counts["total"] = sum(counts.values())
    return {
        "counts": counts,
        "functions": list(owner.functions),
        "outcomes": {status: names for status, names in sorted(outcomes.items())},
        "source": owner.source,
        "test_seams": list(owner.test_seams),
    }


def _baseline_payload(owner_reports: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Create an explicit survivor allowlist from current outcomes."""
    owners: dict[str, Any] = {}
    for owner in OWNERS:
        report = owner_reports[owner.key]
        fatal = sorted(
            mutant_name
            for status in FATAL_STATUSES
            for mutant_name in report["outcomes"].get(status, [])
        )
        if fatal:
            fatal_counts = {
                status: len(report["outcomes"].get(status, []))
                for status in sorted(FATAL_STATUSES)
                if report["outcomes"].get(status)
            }
            raise PilotError(
                f"cannot baseline {owner.key}: non-survivor failures present: {fatal_counts}"
            )
        owners[owner.key] = {
            "accepted_survivors": report["outcomes"].get("survived", []),
            "functions": report["functions"],
            "source": report["source"],
            "test_seams": report["test_seams"],
        }
    return {
        "owners": owners,
        "schema_version": SCHEMA_VERSION,
        "tool": {"name": "mutmut", "version": MUTMUT_VERSION},
    }


def _validate_baseline(payload: dict[str, object]) -> dict[str, dict[str, Any]]:
    """Validate the baseline schema and owner allowlist."""
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise BaselineError("invalid mutation baseline: unsupported schema_version")
    if payload.get("tool") != {"name": "mutmut", "version": MUTMUT_VERSION}:
        raise BaselineError("invalid mutation baseline: tool version mismatch")
    owners = payload.get("owners")
    if not isinstance(owners, dict) or set(owners) != {owner.key for owner in OWNERS}:
        raise BaselineError("invalid mutation baseline: owner set mismatch")

    validated: dict[str, dict[str, Any]] = {}
    for owner in OWNERS:
        entry = owners.get(owner.key)
        if not isinstance(entry, dict):
            raise BaselineError(f"invalid mutation baseline: malformed owner {owner.key}")
        expected_contract = {
            "functions": list(owner.functions),
            "source": owner.source,
            "test_seams": list(owner.test_seams),
        }
        for key, expected in expected_contract.items():
            if entry.get(key) != expected:
                raise BaselineError(
                    f"invalid mutation baseline: {owner.key} {key} does not match allowlist"
                )
        accepted = entry.get("accepted_survivors")
        if not isinstance(accepted, list) or not all(isinstance(name, str) for name in accepted):
            raise BaselineError(
                f"invalid mutation baseline: {owner.key} accepted_survivors must be strings"
            )
        if accepted != sorted(set(accepted)):
            raise BaselineError(
                f"invalid mutation baseline: {owner.key} accepted_survivors must be sorted and unique"
            )
        validated[owner.key] = entry
    return validated


def _compare_with_baseline(
    owner_reports: dict[str, dict[str, Any]],
    baseline_owners: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], bool]:
    """Compare current outcomes with the explicit survivor allowlist."""
    comparisons: dict[str, Any] = {}
    failed = False
    for owner in OWNERS:
        report = owner_reports[owner.key]
        accepted = set(baseline_owners[owner.key]["accepted_survivors"])
        survivors = set(report["outcomes"].get("survived", []))
        unexpected_survivors = sorted(survivors - accepted)
        resolved_survivors = sorted(accepted - survivors)
        fatal_outcomes = {
            status: report["outcomes"].get(status, [])
            for status in sorted(FATAL_STATUSES)
            if report["outcomes"].get(status)
        }
        owner_failed = bool(unexpected_survivors or fatal_outcomes)
        failed |= owner_failed
        comparisons[owner.key] = {
            "fatal_outcomes": fatal_outcomes,
            "resolved_survivors": resolved_survivors,
            "status": "regression" if owner_failed else "accepted",
            "unexpected_survivors": unexpected_survivors,
        }
    return comparisons, failed


def _sanitized_environment() -> dict[str, str]:
    """Remove credential-bearing environment variables before executing mutants."""
    environment = os.environ.copy()
    for name in SENSITIVE_ENVIRONMENT_NAMES:
        environment.pop(name, None)
    return environment


def _run_mutmut(*, max_children: int, reuse_cache: bool, repo_root: Path) -> float:
    """Execute exactly the allowlisted mutmut patterns."""
    if not reuse_cache:
        shutil.rmtree(repo_root / "mutants", ignore_errors=True)
    executable = shutil.which("mutmut")
    if executable is None:
        raise PilotError("mutmut is not installed; run uv sync --extra dev")
    patterns = [pattern for owner in OWNERS for pattern in owner.patterns]
    command = [executable, "run", "--max-children", str(max_children), *patterns]
    started = time.monotonic()
    result = subprocess.run(  # noqa: S603 - fixed executable and owner patterns
        command,
        cwd=repo_root,
        env=_sanitized_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    elapsed = time.monotonic() - started
    # mutmut 3.6.0 returns zero for completed runs even when mutants survive;
    # individual outcomes are authoritative in the metadata read below.
    if result.returncode != 0:
        output = "\n".join(part for part in (result.stdout, result.stderr) if part)
        raise PilotError(
            f"mutmut failed with exit code {result.returncode}\n{_ascii(output).strip()}"
        )
    return elapsed


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write canonical mutation evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
            encoding="ascii",
        )
        temporary.replace(path)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise PilotError(f"failed to write mutation report {path}: {error}") from error


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--max-children", type=int, default=2)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Read existing mutmut metadata without executing mutants.",
    )
    parser.add_argument(
        "--reuse-cache",
        action="store_true",
        help="Reuse the local mutants directory instead of starting fresh.",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="Replace the survivor allowlist with the current clean outcomes.",
    )
    return parser.parse_args()


def main() -> int:
    """Run the pilot, write its report, and enforce the checked-in baseline."""
    args = _parse_args()
    if args.max_children < 1:
        print("[x] --max-children must be at least 1", file=sys.stderr)
        return 2

    try:
        elapsed = 0.0
        if args.report_only:
            print("[!] Report-only mode reuses existing mutmut metadata without executing tests.")
        else:
            mode = "cached" if args.reuse_cache else "fresh"
            print(f"[i] Starting {mode} mutation execution.")
            elapsed = _run_mutmut(
                max_children=args.max_children,
                reuse_cache=args.reuse_cache,
                repo_root=REPO_ROOT,
            )
        owner_reports = {owner.key: _owner_report(owner, REPO_ROOT) for owner in OWNERS}

        if args.update_baseline:
            baseline_payload = _baseline_payload(owner_reports)
            args.baseline.parent.mkdir(parents=True, exist_ok=True)
            write_baseline(args.baseline, baseline_payload, label="mutation")

        baseline_payload = load_baseline(args.baseline, label="mutation")
        baseline_owners = _validate_baseline(baseline_payload)
        comparisons, failed = _compare_with_baseline(owner_reports, baseline_owners)
        report = {
            "comparisons": comparisons,
            "owners": owner_reports,
            "schema_version": SCHEMA_VERSION,
            "status": "regression" if failed else "accepted",
            "tool": {"name": "mutmut", "version": MUTMUT_VERSION},
        }
        _write_report(args.output, report)
    except (BaselineError, PilotError) as error:
        print(f"[x] Mutation pilot failed: {_ascii(str(error))}", file=sys.stderr)
        return 1

    total = sum(report["counts"]["total"] for report in owner_reports.values())
    survivors = sum(report["counts"].get("survived", 0) for report in owner_reports.values())
    runtime = "metadata only" if args.report_only else f"{elapsed:.1f}s runtime"
    print(f"[+] Mutation pilot report: {total} mutants, {survivors} survivors, {runtime}")
    print(f"[i] Report written to {_ascii(str(args.output))}")
    for owner in OWNERS:
        counts = owner_reports[owner.key]["counts"]
        print(
            f"[i] {owner.key}: total={counts['total']} "
            f"killed={counts.get('killed', 0)} survived={counts.get('survived', 0)} "
            f"timeout={counts.get('timeout', 0)} suspicious={counts.get('suspicious', 0)}"
        )
        resolved_count = len(comparisons[owner.key]["resolved_survivors"])
        if resolved_count:
            print(
                f"[i] {owner.key}: {resolved_count} accepted survivor(s) are now killed; "
                "review and rerun with --update-baseline."
            )
    if failed:
        print(
            "[x] Unexpected mutation outcomes are not allowlisted. "
            "Inspect the JSON report, use 'mutmut results' to find each survivor, "
            "then run 'mutmut show <name>'; "
            "add a behavioral test before updating the baseline.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
