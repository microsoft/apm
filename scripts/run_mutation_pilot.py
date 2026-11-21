#!/usr/bin/env python3
"""Mutation-test canonical owner modules and enforce their recorded baseline.

Each owner names one source file, the functions to mutate and the test seams
expected to kill them (#4121). ``tests/mutation/baseline.json`` records, per
owner, a kill-rate floor and every surviving mutant: equivalent survivors carry
a reason, and only the five original pilot owners keep an unexplained legacy
allowlist. A run fails on a new survivor, a kill rate below its floor, or an
incomplete outcome. ``--owner`` runs one owner (the scheduled job shards on it)
and ``--negative-control`` proves that losing one killed mutant fails the floor.
"""

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

from apm_cli.utils.atomic_io import atomic_write_text

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = REPO_ROOT / "tests" / "mutation" / "baseline.json"
DEFAULT_OUTPUT = REPO_ROOT / "build" / "mutation-pilot" / "report.json"
MUTMUT_VERSION = "3.6.0"
CLASS_NAME_SEPARATOR = chr(0x01C1)
SCHEMA_VERSION = 2

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
            "tests/unit/policy/test_cache_merged_effective_logic.py"
            "::test_policy_serializer_covers_every_dataclass_leaf",
            "tests/unit/policy/test_cache_merged_effective_logic.py"
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
            "tests/unit/compilation/test_link_resolver_resolution.py",
        ),
    ),
    Owner(
        # Scoped to the two fail-closed field normalizers ONLY -- this
        # owner does NOT cover LockedDependency.to_dict/from_dict/
        # to_dependency_ref. mutmut 3.6.0 structurally skips every method
        # of a `@dataclass`-decorated class (file_mutation.py's
        # `_skip_node_and_children` returns True for any decorated
        # ClassDef), and both LockedDependency and LockFile carry
        # `@dataclass` -- so their methods can never be mutated regardless
        # of `only_mutate`/pattern configuration. Confirmed empirically:
        # `mutmut run` filtered to those method patterns raises
        # `AssertionError: Filtered for specific mutants, but nothing
        # matches`, and a minimal reproduction without `@dataclass`
        # restores normal per-mutant trampolining.
        #
        # This is a RECORDED FOLLOW-UP CONSTRAINT, not a gap this owner
        # silently ignores: PR #2246 added seven manual mutation-break
        # twins in the test seam below (see
        # test_*_fail_closed_property_breaks_if_normalizer_is_bypassed and
        # siblings) that hand-assert against specific mutation scenarios
        # for the decorated-dataclass methods mutmut cannot reach. Those
        # twins are the current defense for that surface; automated
        # coverage there remains blocked on the upstream mutmut limitation
        # above, not on missing test authorship.
        key="lockfile-normalization",
        source="src/apm_cli/deps/lockfile.py",
        functions=(
            "_normalize_lockfile_host_type",
            "_normalize_exec_status",
        ),
        test_seams=("tests/unit/deps/test_lockfile_field_properties.py",),
    ),
)


# The five original pilot owners keep their unexplained survivor allowlist;
# every later owner kills each survivor or records why it is equivalent.
LEGACY_OWNER_KEYS = frozenset(
    {
        "subset-selection",
        "update-plan",
        "policy-serialization",
        "link-projection",
        "lockfile-normalization",
    }
)

# Owners from the 0.37 owner moves (#4121 wave A), by blast radius.
OWNERS += (
    Owner(
        key="policy-outcome-routing",
        source="src/apm_cli/policy/outcome_routing.py",
        functions=(
            "classify_discovery_outcome",
            "outcome_cause",
            "route_discovery_outcome",
            "classify_project_policy",
            "resolve_trust_policy",
            "discover_trust_policy",
            "_report_miss_neutrally",
            "_TrustReaderLog.flush",
            "_TrustReaderLog.policy_discovery_miss",
        ),
        test_seams=("tests/unit/policy/test_outcome_routing.py",),
    ),
    Owner(
        key="executable-trust-context",
        source="src/apm_cli/security/executables.py",
        functions=(
            "resolve_exec_decision",
            "authorized_exec_map",
            "package_type_approved",
            "compose_trust_bin",
            "exec_trust_context_for_project",
            "effective_exec_map_for_project",
            "resolve_project_exec_allow_map",
        ),
        test_seams=(
            "tests/unit/security/test_resolve_exec_decision.py",
            "tests/unit/security/test_exec_trust_context.py",
        ),
    ),
    Owner(
        key="install-entry-routing",
        source="src/apm_cli/install/pipeline_entry.py",
        functions=("record_preview_resolution", "pipeline_preview", "install_pipeline_options"),
        test_seams=("tests/unit/install/test_pipeline_entry.py",),
    ),
    Owner(
        key="install-commit-order",
        source="src/apm_cli/install/commit_gate.py",
        functions=(
            "policy_subject_mcp_deps",
            "enforce_mcp_policy",
            "refuse_blocked_mcp",
            "_resolved_rows",
            "prospective_lockfile",
            "_check_merged_mcp_policy",
            "check_before_resolve",
            "check_before_deploy",
            "check_before_lockfile",
        ),
        test_seams=("tests/unit/install/test_commit_gate.py",),
    ),
    Owner(
        # lockfile_write_scope is a @contextmanager: mutmut 3.6.0 skips every
        # function with a decorator other than staticmethod/classmethod, so
        # the scope's commit/discard choice is covered through _commits and
        # _commit, which it calls.
        key="lockfile-write-authority",
        source="src/apm_cli/deps/lockfile_write_authority.py",
        functions=(
            "_scope",
            "_refused",
            "_commits",
            "stage_lockfile",
            "staged_lockfile_text",
            "has_staged_lockfile",
            "lockfile_exists",
            "remove_lockfile",
            "restore_lockfile_bytes",
            "after_lockfile_commit",
            "flush_lockfile_writes",
            "_commit",
        ),
        test_seams=("tests/unit/deps/test_lockfile_write_authority.py",),
    ),
    Owner(
        key="apm-home-resolution",
        source="src/apm_cli/core/scope.py",
        functions=(
            "get_apm_home",
            "is_user_scope",
            "set_source_root_override",
            "get_source_root",
            "get_deploy_root",
            "get_workspace_deploy_root",
            "get_apm_dir",
            "manifest_file",
            "manifest_root",
            "scope_manifest_path",
            "is_user_scope_root",
            "policy_discovery_uses_git_remote",
            "get_modules_dir",
            "get_manifest_path",
            "get_lockfile_dir",
        ),
        test_seams=("tests/unit/core/test_apm_home_resolution.py",),
    ),
    Owner(
        key="egress-route-policy",
        source="src/apm_cli/deps/egress_route.py",
        functions=("route_for", "is_direct_exception", "is_refused", "is_proxy_only"),
        test_seams=("tests/unit/deps/test_egress_route.py",),
    ),
    Owner(
        key="remediation-advice",
        source="src/apm_cli/core/remediation.py",
        functions=("_sentence", "either", "either_sentence", "kept_note", "kept_file_message"),
        test_seams=("tests/unit/core/test_remediation.py",),
    ),
    Owner(
        key="credential-origin-scope",
        source="src/apm_cli/utils/http_credentials.py",
        functions=(
            "strip_cross_origin_credential_headers",
            "origin_bound_refusal",
            "_origin_key",
            "_comparable_paths",
            "_path_within",
            "_comparable_host",
        ),
        test_seams=(
            "tests/unit/utils/test_http_credentials.py",
            "tests/unit/utils/test_http_credentials_owner.py",
        ),
    ),
    Owner(
        key="url-path-containment-decoding",
        source="src/apm_cli/utils/path_security.py",
        functions=(
            "decode_url_path_segments",
            "has_symlink_component",
            "ensure_path_within",
        ),
        test_seams=(
            "tests/unit/test_path_security.py",
            "tests/unit/test_path_security_io.py",
        ),
    ),
    Owner(
        key="network-host-loopback-classification",
        source="src/apm_cli/utils/net.py",
        functions=(
            "parse_host_address",
            "is_loopback_host",
        ),
        test_seams=("tests/unit/utils/test_net.py",),
    ),
    Owner(
        key="host-credential-resolution",
        source="src/apm_cli/core/auth_outcome.py",
        functions=(
            "classify_auth_outcome",
            "is_ado_signin_location",
            "_classify_response",
            "_transport_kind",
        ),
        test_seams=(
            "tests/unit/core/test_auth_outcome_owner.py",
            "tests/unit/core/test_auth_outcome.py",
        ),
    ),
    Owner(
        key="deterministic-atomic-text-writes",
        source="src/apm_cli/utils/atomic_io.py",
        functions=(
            "normalize_crlf_to_lf",
            "write_text_lf",
            "atomic_write_text",
            "_validate_temp_name_fragment",
        ),
        test_seams=(
            "tests/unit/utils/test_atomic_io.py",
            "tests/unit/utils/test_atomic_io_owner.py",
        ),
    ),
    Owner(
        key="frontmatter-bom-bounded-yaml",
        source="src/apm_cli/utils/yaml_io.py",
        functions=(
            "loads_frontmatter",
            "load_frontmatter",
            "_bounded_load",
        ),
        test_seams=(
            "tests/unit/test_yaml_io.py",
            "tests/unit/utils/test_frontmatter_horizontal_rules.py",
            "tests/unit/utils/test_frontmatter_owner.py",
        ),
    ),
    Owner(
        key="read-only-lockfile-path",
        source="src/apm_cli/deps/lockfile.py",
        functions=(
            "resolve_lockfile_path_for_read",
            "resolve_reproducible_timestamp",
        ),
        test_seams=("tests/unit/deps/test_lockfile_read_path.py",),
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
    except (
        FileNotFoundError,
        IsADirectoryError,
        PermissionError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as error:
        raise PilotError(f"invalid mutmut metadata {meta_path}: {error}") from error

    if not isinstance(payload, dict):
        raise PilotError(f"invalid mutmut metadata {meta_path}: root must be an object")
    exit_codes = payload.get("exit_code_by_key")
    if not isinstance(exit_codes, dict):
        raise PilotError(f"invalid mutmut metadata {meta_path}: exit_code_by_key missing")

    scoped: dict[str, int | None] = {}
    for raw_name, exit_code in exit_codes.items():
        if not isinstance(raw_name, str):
            raise PilotError(f"invalid mutmut metadata {meta_path}: non-string mutant name")
        if not any(fnmatch.fnmatchcase(raw_name, pattern) for pattern in owner.patterns):
            continue
        if exit_code is not None and (
            not isinstance(exit_code, int) or isinstance(exit_code, bool)
        ):
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


def kill_rate(report: dict[str, Any]) -> float:
    """Return killed / (killed + survived) for one owner report (1.0 when nothing ran)."""
    killed = len(report["outcomes"].get("killed", []))
    survived = len(report["outcomes"].get("survived", []))
    return 1.0 if killed + survived == 0 else killed / (killed + survived)


def _floor(rate: float) -> float:
    """Round a measured kill rate down to the four decimals the baseline records."""
    return int(rate * 10_000) / 10_000


def _fatal_outcomes(report: dict[str, Any]) -> dict[str, list[str]]:
    return {
        status: report["outcomes"].get(status, [])
        for status in sorted(FATAL_STATUSES)
        if report["outcomes"].get(status)
    }


def _baseline_payload(
    owner_reports: dict[str, dict[str, Any]],
    previous_owners: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Record each owner's survivors and kill-rate floor from clean outcomes.

    Legacy pilot owners keep an unexplained ``accepted_survivors`` allowlist.
    Every other owner may only carry survivors already recorded in
    ``equivalent_survivors`` with a reason; a new survivor needs a test or a
    reason before the baseline can be written.
    """
    owners: dict[str, Any] = {}
    for owner in OWNERS:
        report = owner_reports[owner.key]
        fatal = _fatal_outcomes(report)
        if fatal:
            fatal_counts = {status: len(names) for status, names in fatal.items()}
            raise PilotError(
                f"cannot baseline {owner.key}: non-survivor failures present: {fatal_counts}"
            )
        survivors = report["outcomes"].get("survived", [])
        previous = (previous_owners or {}).get(owner.key, {})
        reasons = previous.get("equivalent_survivors", {})
        if owner.key in LEGACY_OWNER_KEYS:
            accepted, equivalent = survivors, {}
        else:
            unexplained = sorted(name for name in survivors if name not in reasons)
            if unexplained:
                raise PilotError(
                    f"cannot baseline {owner.key}: {len(unexplained)} survivor(s) have no test "
                    f"and no equivalent reason: {unexplained}"
                )
            accepted, equivalent = [], {name: reasons[name] for name in sorted(survivors)}
        owners[owner.key] = {
            "accepted_survivors": accepted,
            "equivalent_survivors": equivalent,
            "functions": report["functions"],
            "kill_rate_floor": _floor(kill_rate(report)),
            "source": report["source"],
            "test_seams": report["test_seams"],
        }
    return {
        "owners": owners,
        "schema_version": SCHEMA_VERSION,
        "tool": {"name": "mutmut", "version": MUTMUT_VERSION},
    }


def _validate_owner_entry(owner: Owner, entry: object) -> dict[str, Any]:
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
    if accepted and owner.key not in LEGACY_OWNER_KEYS:
        raise BaselineError(
            f"invalid mutation baseline: {owner.key} may not accept unexplained survivors; "
            "kill them or record equivalent_survivors with a reason"
        )
    equivalent = entry.get("equivalent_survivors")
    if not isinstance(equivalent, dict) or not all(
        isinstance(name, str) and isinstance(reason, str) and reason.strip()
        for name, reason in equivalent.items()
    ):
        raise BaselineError(
            f"invalid mutation baseline: {owner.key} equivalent_survivors must map each "
            "mutant to a non-empty reason"
        )
    floor = entry.get("kill_rate_floor")
    if not isinstance(floor, int | float) or isinstance(floor, bool) or not 0 <= floor <= 1:
        raise BaselineError(
            f"invalid mutation baseline: {owner.key} kill_rate_floor must be between 0 and 1"
        )
    return entry


def _validate_baseline(payload: dict[str, object]) -> dict[str, dict[str, Any]]:
    """Validate the baseline schema and owner allowlist."""
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise BaselineError("invalid mutation baseline: unsupported schema_version")
    if payload.get("tool") != {"name": "mutmut", "version": MUTMUT_VERSION}:
        raise BaselineError("invalid mutation baseline: tool version mismatch")
    owners = payload.get("owners")
    if not isinstance(owners, dict) or set(owners) != {owner.key for owner in OWNERS}:
        raise BaselineError("invalid mutation baseline: owner set mismatch")
    return {owner.key: _validate_owner_entry(owner, owners.get(owner.key)) for owner in OWNERS}


def _compare_owner(report: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    """Compare one owner's outcomes with its survivor allowlist and kill-rate floor."""
    allowed = set(entry["accepted_survivors"]) | set(entry["equivalent_survivors"])
    survivors = set(report["outcomes"].get("survived", []))
    unexpected_survivors = sorted(survivors - allowed)
    fatal_outcomes = _fatal_outcomes(report)
    rate = kill_rate(report)
    below_floor = rate < entry["kill_rate_floor"]
    failed = bool(unexpected_survivors or fatal_outcomes or below_floor)
    return {
        "fatal_outcomes": fatal_outcomes,
        "kill_rate": round(rate, 4),
        "kill_rate_floor": entry["kill_rate_floor"],
        "resolved_survivors": sorted(allowed - survivors),
        "status": "regression" if failed else "accepted",
        "unexpected_survivors": unexpected_survivors,
    }


def _compare_with_baseline(
    owner_reports: dict[str, dict[str, Any]],
    baseline_owners: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], bool]:
    """Compare every reported owner with the explicit survivor allowlist and floor."""
    comparisons = {
        key: _compare_owner(report, baseline_owners[key]) for key, report in owner_reports.items()
    }
    return comparisons, any(c["status"] == "regression" for c in comparisons.values())


def negative_control(report: dict[str, Any], entry: dict[str, Any]) -> bool:
    """Return True when losing one killed mutant would fail this owner's floor check.

    The flipped mutant is also allowed as a survivor, so only the kill-rate
    floor can flag the weakened run: this proves the floor itself enforces.
    """
    killed = report["outcomes"].get("killed", [])
    if not killed:
        return entry["kill_rate_floor"] > 0
    weakened = {
        **report,
        "outcomes": {
            **report["outcomes"],
            "killed": killed[1:],
            "survived": [*report["outcomes"].get("survived", []), killed[0]],
        },
    }
    allowing = {
        **entry,
        "equivalent_survivors": {**entry["equivalent_survivors"], killed[0]: "negative control"},
    }
    return _compare_owner(weakened, allowing)["status"] == "regression"


def _sanitized_environment() -> dict[str, str]:
    """Remove credential-bearing environment variables before executing mutants."""
    environment = os.environ.copy()
    for name in SENSITIVE_ENVIRONMENT_NAMES:
        environment.pop(name, None)
    return environment


def _run_mutmut(
    *,
    max_children: int,
    reuse_cache: bool,
    repo_root: Path,
    owners: tuple[Owner, ...] = OWNERS,
) -> float:
    """Execute exactly the allowlisted mutmut patterns of *owners*."""
    if not reuse_cache:
        shutil.rmtree(repo_root / "mutants", ignore_errors=True)
    executable = shutil.which("mutmut")
    if executable is None:
        raise PilotError("mutmut is not installed; run uv sync --extra dev")
    patterns = [pattern for owner in owners for pattern in owner.patterns]
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


def run_owners(
    owners: tuple[Owner, ...],
    *,
    max_children: int = 2,
    reuse_cache: bool = False,
    repo_root: Path = REPO_ROOT,
) -> dict[str, dict[str, Any]]:
    """Mutate *owners* and return each one's outcome report.

    The reusable entry point for other fault-injection harnesses (#4514): an
    ``Owner`` names a source file, the functions to mutate and the test seams
    expected to kill them; the report lists every mutant by outcome.
    """
    _run_mutmut(
        max_children=max_children, reuse_cache=reuse_cache, repo_root=repo_root, owners=owners
    )
    return {owner.key: _owner_report(owner, repo_root) for owner in owners}


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write canonical mutation evidence.

    Routes through :func:`apm_cli.utils.atomic_io.atomic_write_text`, the
    single canonical atomic-write primitive, so the on-disk bytes use LF
    line endings on every platform (Windows text-mode writes would
    otherwise translate ``\\n`` to ``\\r\\n``, see microsoft/apm#2233).
    The payload is serialized with ``ensure_ascii=True`` so the resulting
    text -- and therefore its UTF-8 encoding -- contains only printable
    ASCII bytes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    try:
        atomic_write_text(path, content)
    except OSError as error:
        raise PilotError(f"failed to write mutation report {path}: {error}") from error


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--max-children", type=int, default=2)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--owner",
        action="append",
        default=[],
        metavar="KEY",
        help="Mutate only this owner (repeatable); default: every owner.",
    )
    parser.add_argument(
        "--list-owners",
        action="store_true",
        help="Print the owner keys as a JSON array and exit.",
    )
    parser.add_argument(
        "--negative-control",
        action="store_true",
        help="Also prove that losing one killed mutant fails each owner's floor.",
    )
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
        help="Rewrite survivors and kill-rate floors from the current clean outcomes.",
    )
    return parser.parse_args()


def select_owners(keys: list[str]) -> tuple[Owner, ...]:
    """Return the owners named by *keys*, or every owner when none is named."""
    if not keys:
        return OWNERS
    by_key = {owner.key: owner for owner in OWNERS}
    unknown = sorted(set(keys) - set(by_key))
    if unknown:
        raise PilotError(f"unknown owner(s): {unknown}; known: {sorted(by_key)}")
    return tuple(owner for owner in OWNERS if owner.key in keys)


def _print_summary(
    owner_reports: dict[str, dict[str, Any]], comparisons: dict[str, Any], elapsed: float
) -> None:
    total = sum(report["counts"]["total"] for report in owner_reports.values())
    survivors = sum(report["counts"].get("survived", 0) for report in owner_reports.values())
    runtime = "metadata only" if elapsed < 0 else f"{elapsed:.1f}s runtime"
    print(f"[+] Mutation report: {total} mutants, {survivors} survivors, {runtime}")
    for key, report in owner_reports.items():
        counts = report["counts"]
        comparison = comparisons[key]
        print(
            f"[i] {key}: total={counts['total']} killed={counts.get('killed', 0)} "
            f"survived={counts.get('survived', 0)} kill_rate={comparison['kill_rate']:.4f} "
            f"floor={comparison['kill_rate_floor']:.4f}"
        )
        if comparison["resolved_survivors"]:
            print(
                f"[i] {key}: {len(comparison['resolved_survivors'])} recorded survivor(s) are "
                "now killed; review and rerun with --update-baseline."
            )


def main() -> int:
    """Run the mutation owners, write the report, and enforce the checked-in baseline."""
    args = _parse_args()
    if args.list_owners:
        print(json.dumps([owner.key for owner in OWNERS]))
        return 0
    if args.max_children < 1:
        print("[x] --max-children must be at least 1", file=sys.stderr)
        return 2

    try:
        owners = select_owners(args.owner)
        if args.update_baseline and owners != OWNERS:
            raise PilotError("--update-baseline needs every owner; drop --owner")
        elapsed = -1.0
        if args.report_only:
            print("[!] Report-only mode reuses existing mutmut metadata without executing tests.")
            owner_reports = {owner.key: _owner_report(owner, REPO_ROOT) for owner in owners}
        else:
            mode = "cached" if args.reuse_cache else "fresh"
            print(f"[i] Starting {mode} mutation execution for {len(owners)} owner(s).")
            started = time.monotonic()
            owner_reports = run_owners(
                owners, max_children=args.max_children, reuse_cache=args.reuse_cache
            )
            elapsed = time.monotonic() - started

        if args.update_baseline:
            previous = None
            if args.baseline.exists():
                previous = load_baseline(args.baseline, label="mutation").get("owners")
            args.baseline.parent.mkdir(parents=True, exist_ok=True)
            write_baseline(
                args.baseline, _baseline_payload(owner_reports, previous), label="mutation"
            )

        baseline_owners = _validate_baseline(load_baseline(args.baseline, label="mutation"))
        comparisons, failed = _compare_with_baseline(owner_reports, baseline_owners)
        _write_report(
            args.output,
            {
                "comparisons": comparisons,
                "owners": owner_reports,
                "schema_version": SCHEMA_VERSION,
                "status": "regression" if failed else "accepted",
                "tool": {"name": "mutmut", "version": MUTMUT_VERSION},
            },
        )
        uncontrolled = [
            key
            for key, report in owner_reports.items()
            if args.negative_control and not negative_control(report, baseline_owners[key])
        ]
    except (BaselineError, PilotError) as error:
        print(f"[x] Mutation run failed: {_ascii(str(error))}", file=sys.stderr)
        return 1

    _print_summary(owner_reports, comparisons, elapsed)
    print(f"[i] Report written to {_ascii(str(args.output))}")
    if args.negative_control:
        if uncontrolled:
            print(
                f"[x] Negative control: losing one killed mutant would not fail {uncontrolled}; "
                "the floor check is not enforcing.",
                file=sys.stderr,
            )
            return 1
        print(f"[+] Negative control: losing one killed mutant fails every owner ({len(owners)}).")
    if failed:
        print(
            "[x] Mutation outcomes regressed: a new survivor, a kill rate below its floor, or "
            "an incomplete run. Inspect the JSON report, use 'mutmut results' to find each "
            "survivor, then run 'mutmut show <name>'; add a behavioral test, or record an "
            "equivalent mutant with its reason, before updating the baseline.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
