#!/usr/bin/env python3
"""Run owner-scoped mutation testing and enforce per-owner kill-rate floors.

``tests/mutation/floors.json`` is the single source of truth for which owner
functions are mutated, which test seams may kill them, the recorded floor per
owner, and the reviewed equivalent mutants (with reasons).
"""

from __future__ import annotations

import argparse
import fnmatch
import importlib.util
import json
import math
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ratchet_baseline import BaselineError, load_baseline, load_baseline_at_ref, write_baseline

from apm_cli.utils.atomic_io import atomic_write_text

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FLOORS = REPO_ROOT / "tests" / "mutation" / "floors.json"
DEFAULT_OUTPUT = REPO_ROOT / "build" / "mutation-floors" / "report.json"
MUTMUT_VERSION = "3.6.0"
CLASS_NAME_SEPARATOR = chr(0x01C1)
SCHEMA_VERSION = 2
KILL_RATE_DEFINITION = "(killed + timeout) / (total - equivalent)"

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
DETECTED_STATUSES = frozenset({"killed", "timeout"})
UNDETECTED_STATUSES = frozenset({"no_tests", "survived"})
FATAL_STATUSES = frozenset(
    {
        "interrupted",
        "not_checked",
        "segfault",
        "skipped",
        "suspicious",
        "terminated",
        "type_checked",
        "unknown",
    }
)
EQUIVALENT_CLASSES = frozenset(
    {"defensive-unreachable", "semantic-equivalent", "unobservable-diagnostic-text"}
)
# User-facing error, warning and remediation text is a promise: only
# debug-level or log-only text may be declared unobservable.
DIAGNOSTIC_REASON_PREFIXES = ("debug:", "log-only:")
OWNER_KEYS = frozenset(
    {"architecture_owner", "equivalents", "floor", "measured", "targets", "test_seams"}
)
MEASURED_KEYS = (
    "equivalent",
    "killed",
    "no_tests",
    "survived",
    "timeout",
    "total",
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


# mutmut 3.6.0 resolves the relative ``source_paths`` against the current
# directory on every stats hit, so any test that chdirs (monkeypatch.chdir)
# crashes stats collection with FileNotFoundError. With max_stack_depth=-1 the
# resolved paths are unused, so record the hit anyway.
_MUTMUT_SHIM = """\
import sys
import mutmut
import mutmut.mutation.trampoline as trampoline
from mutmut.__main__ import cli
_hit = trampoline.record_trampoline_hit
def _cwd_safe_hit(name):
    try:
        _hit(name)
    except FileNotFoundError:
        mutmut._stats.add(name)
trampoline.record_trampoline_hit = _cwd_safe_hit
sys.exit(cli())
"""


class PilotError(RuntimeError):
    """Raised when mutation evidence cannot be trusted."""


@dataclass(frozen=True)
class Target:
    """One source file and the exact functions mutated in it."""

    source: str
    functions: tuple[str, ...]

    @property
    def module(self) -> str:
        """Return the import path corresponding to the source file."""
        return self.source.removeprefix("src/").removesuffix(".py").replace("/", ".")

    @property
    def patterns(self) -> tuple[str, ...]:
        """Return exact mutmut function patterns for this target."""
        patterns = []
        for function in self.functions:
            class_name, separator, function_name = function.partition(".")
            if separator:
                mangled = (
                    f"x{CLASS_NAME_SEPARATOR}{class_name}{CLASS_NAME_SEPARATOR}{function_name}"
                )
            else:
                mangled = f"x_{function}"
            patterns.append(f"{self.module}.{mangled}__mutmut_*")
        return tuple(patterns)


@dataclass(frozen=True)
class Owner:
    """One mutation-floor owner recorded in floors.json."""

    key: str
    architecture_owner: str | None
    targets: tuple[Target, ...]
    test_seams: tuple[str, ...]
    floor: float
    equivalents: dict[str, dict[str, str]]

    @property
    def patterns(self) -> tuple[str, ...]:
        """Return every mutmut pattern owned by this entry."""
        return tuple(pattern for target in self.targets for pattern in target.patterns)


def _ascii(text: str) -> str:
    """Return printable ASCII for terminal-safe diagnostics."""
    return text.encode("ascii", errors="backslashreplace").decode("ascii")


def _string_list(value: object, *, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
        raise BaselineError(f"invalid mutation floors: {where} must be a non-empty string list")
    if len(set(value)) != len(value):
        raise BaselineError(f"invalid mutation floors: {where} must be unique")
    return tuple(value)


def _parse_targets(key: str, raw: object) -> tuple[Target, ...]:
    if not isinstance(raw, list) or not raw:
        raise BaselineError(f"invalid mutation floors: {key} targets must be a non-empty list")
    targets = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"functions", "source"}:
            raise BaselineError(f"invalid mutation floors: {key} target is malformed")
        source = item["source"]
        if not isinstance(source, str) or not source.startswith("src/"):
            raise BaselineError(f"invalid mutation floors: {key} target source must be under src/")
        functions = _string_list(item["functions"], where=f"{key} {source} functions")
        targets.append(Target(source=source, functions=functions))
    return tuple(targets)


def _parse_equivalent(key: str, item: object) -> tuple[str, dict[str, str]]:
    if not isinstance(item, dict) or set(item) != {"class", "mutant", "reason"}:
        raise BaselineError(f"invalid mutation floors: {key} equivalent is malformed")
    mutant, kind, reason = item["mutant"], item["class"], item["reason"]
    if not isinstance(mutant, str) or "__mutmut_" not in mutant:
        raise BaselineError(f"invalid mutation floors: {key} equivalent mutant name")
    if kind not in EQUIVALENT_CLASSES:
        raise BaselineError(f"invalid mutation floors: {key} {mutant} class {kind!r}")
    if not isinstance(reason, str) or len(reason.strip()) < 20:
        raise BaselineError(f"invalid mutation floors: {key} {mutant} needs a reason")
    if kind == "unobservable-diagnostic-text" and not reason.startswith(DIAGNOSTIC_REASON_PREFIXES):
        raise BaselineError(
            f"invalid mutation floors: {key} {mutant} diagnostic-text reason must start "
            "with 'debug:' or 'log-only:' and name where it is emitted"
        )
    return mutant, {"class": kind, "reason": reason}


def _parse_equivalents(key: str, raw: object) -> dict[str, dict[str, str]]:
    if not isinstance(raw, list):
        raise BaselineError(f"invalid mutation floors: {key} equivalents must be a list")
    parsed = [_parse_equivalent(key, item) for item in raw]
    names = [name for name, _ in parsed]
    if names != sorted(set(names)):
        raise BaselineError(f"invalid mutation floors: {key} equivalents must be sorted, unique")
    return dict(parsed)


def _parse_floor(key: str, raw: object) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 0 <= raw <= 1:
        raise BaselineError(f"invalid mutation floors: {key} floor must be within [0, 1]")
    return float(raw)


def _parse_owner(key: str, entry: object) -> Owner:
    if not isinstance(entry, dict) or set(entry) != OWNER_KEYS:
        raise BaselineError(f"invalid mutation floors: malformed owner {key}")
    architecture_owner = entry["architecture_owner"]
    if architecture_owner is not None and not isinstance(architecture_owner, str):
        raise BaselineError(f"invalid mutation floors: {key} architecture_owner")
    return Owner(
        key=key,
        architecture_owner=architecture_owner,
        targets=_parse_targets(key, entry["targets"]),
        test_seams=_string_list(entry["test_seams"], where=f"{key} test_seams"),
        floor=_parse_floor(key, entry["floor"]),
        equivalents=_parse_equivalents(key, entry["equivalents"]),
    )


def parse_floors(payload: dict[str, object]) -> tuple[Owner, ...]:
    """Validate a floors document and return its owners in key order."""
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise BaselineError("invalid mutation floors: unsupported schema_version")
    if payload.get("tool") != {"name": "mutmut", "version": MUTMUT_VERSION}:
        raise BaselineError("invalid mutation floors: tool version mismatch")
    if payload.get("kill_rate_definition") != KILL_RATE_DEFINITION:
        raise BaselineError("invalid mutation floors: kill_rate_definition mismatch")
    owners = payload.get("owners")
    if not isinstance(owners, dict) or not owners:
        raise BaselineError("invalid mutation floors: owners must be a non-empty object")
    return tuple(_parse_owner(key, owners[key]) for key in sorted(owners))


def _canonical_mutant_name(raw_name: str) -> str:
    """Replace mutmut's internal mangling with a stable ASCII name."""
    module, separator, key = raw_name.rpartition(".")
    if not separator:
        raise PilotError(f"invalid mutmut name: {_ascii(raw_name)}")
    if key.startswith("x_"):
        return f"{module}.{key[2:]}"
    class_prefix = f"x{CLASS_NAME_SEPARATOR}"
    if key.startswith(class_prefix) and CLASS_NAME_SEPARATOR in key[len(class_prefix) :]:
        class_name, function_mutant = key[len(class_prefix) :].split(
            CLASS_NAME_SEPARATOR, maxsplit=1
        )
        return f"{module}.{class_name}.{function_mutant}"
    raise PilotError(f"invalid mutmut name: {_ascii(raw_name)}")


def _read_meta(meta_path: Path) -> dict[str, Any]:
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
    return exit_codes


def _load_exit_codes(owner: Owner, repo_root: Path) -> dict[str, int | None]:
    """Load and scope one owner's mutmut metadata across its targets."""
    scoped: dict[str, int | None] = {}
    for target in owner.targets:
        meta_path = repo_root / "mutants" / f"{target.source}.meta"
        for raw_name, exit_code in _read_meta(meta_path).items():
            if not isinstance(raw_name, str):
                raise PilotError(f"invalid mutmut metadata {meta_path}: non-string mutant name")
            if not any(fnmatch.fnmatchcase(raw_name, pattern) for pattern in target.patterns):
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


def _outcomes(exit_codes: dict[str, int | None]) -> dict[str, list[str]]:
    outcomes: dict[str, list[str]] = {}
    for mutant_name, exit_code in sorted(exit_codes.items()):
        status = STATUS_BY_EXIT_CODE.get(exit_code, "unknown")
        outcomes.setdefault(status, []).append(mutant_name)
    return outcomes


def _measure(owner: Owner, outcomes: dict[str, list[str]]) -> dict[str, Any]:
    """Compute counts and the kill rate for one owner's outcomes."""
    undetected = {name for status in UNDETECTED_STATUSES for name in outcomes.get(status, [])}
    equivalent = sorted(undetected & set(owner.equivalents))
    counts = {key: 0 for key in MEASURED_KEYS}
    for status, names in outcomes.items():
        if status in counts:
            counts[status] = len(names)
    counts["total"] = sum(len(names) for names in outcomes.values())
    counts["equivalent"] = len(equivalent)
    detected = counts["killed"] + counts["timeout"]
    denominator = counts["total"] - counts["equivalent"]
    counts["kill_rate"] = round(detected / denominator, 4) if denominator else 1.0
    return counts


def owner_report(owner: Owner, outcomes: dict[str, list[str]]) -> dict[str, Any]:
    """Build the deterministic comparison of one owner against its floor."""
    measured = _measure(owner, outcomes)
    undetected = {name for status in UNDETECTED_STATUSES for name in outcomes.get(status, [])}
    present = {name for names in outcomes.values() for name in names}
    fatal = {status: outcomes[status] for status in sorted(FATAL_STATUSES) if outcomes.get(status)}
    below_floor = measured["kill_rate"] < owner.floor
    return {
        "architecture_owner": owner.architecture_owner,
        "fatal_outcomes": fatal,
        "floor": owner.floor,
        "killed_equivalents": sorted((set(owner.equivalents) & present) - undetected),
        "measured": measured,
        "outcomes": {status: names for status, names in sorted(outcomes.items())},
        "stale_equivalents": sorted(set(owner.equivalents) - present),
        "status": "regression" if (below_floor or fatal) else "accepted",
        "unlisted_survivors": sorted(undetected - set(owner.equivalents)),
    }


def _sanitized_environment() -> dict[str, str]:
    """Remove credential-bearing environment variables before executing mutants."""
    environment = os.environ.copy()
    for name in SENSITIVE_ENVIRONMENT_NAMES:
        environment.pop(name, None)
    return environment


def _run_mutmut(
    owners: tuple[Owner, ...], *, max_children: int, reuse_cache: bool, repo_root: Path
) -> float:
    """Execute exactly the owners' function patterns."""
    if not reuse_cache:
        shutil.rmtree(repo_root / "mutants", ignore_errors=True)
    if importlib.util.find_spec("mutmut") is None:
        raise PilotError("mutmut is not installed; run uv sync --extra dev")
    patterns = [pattern for owner in owners for pattern in owner.patterns]
    command = [
        sys.executable,
        "-c",
        _MUTMUT_SHIM,
        "run",
        "--max-children",
        str(max_children),
        *patterns,
    ]
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
    # individual outcomes are authoritative in the metadata read afterwards.
    if result.returncode != 0:
        output = "\n".join(part for part in (result.stdout, result.stderr) if part)
        raise PilotError(
            f"mutmut failed with exit code {result.returncode}\n{_ascii(output).strip()}"
        )
    return elapsed


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write canonical, printable-ASCII mutation evidence (LF endings)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    try:
        atomic_write_text(path, content)
    except OSError as error:
        raise PilotError(f"failed to write mutation report {path}: {error}") from error


def truncated_floor(kill_rate: float) -> float:
    """Return the kill rate truncated (never rounded up) to two decimals."""
    return math.floor(kill_rate * 100 + 1e-9) / 100


def updated_floors_payload(
    payload: dict[str, Any], reports: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Record measured rates; floors only ever rise."""
    updated = json.loads(json.dumps(payload))
    for key, report in reports.items():
        if report["fatal_outcomes"]:
            raise PilotError(f"cannot update floors for {key}: non-survivor failures present")
        entry = updated["owners"][key]
        measured = report["measured"]
        entry["measured"] = measured
        entry["floor"] = max(float(entry["floor"]), truncated_floor(measured["kill_rate"]))
    return updated


def check_floor_ratchet(current: dict[str, Any], base: dict[str, Any] | None) -> list[str]:
    """Return ratchet violations: floors never lower and owners never disappear."""
    parse_floors(current)
    if base is None:
        return []
    base_owners = base.get("owners")
    if not isinstance(base_owners, dict):
        return []
    current_owners = current["owners"]
    problems = []
    for key, base_entry in sorted(base_owners.items()):
        entry = current_owners.get(key)
        if entry is None:
            problems.append(f"{key}: owner removed from mutation floors")
            continue
        base_floor = base_entry.get("floor") if isinstance(base_entry, dict) else None
        if isinstance(base_floor, (int, float)) and entry["floor"] < base_floor:
            problems.append(f"{key}: floor lowered from {base_floor} to {entry['floor']}")
    return problems


def negative_control(owner: Owner, outcomes: dict[str, list[str]]) -> dict[str, Any]:
    """Simulate a test-suite regression by turning every detected mutant into a survivor."""
    simulated = {
        status: list(names) for status, names in outcomes.items() if status not in DETECTED_STATUSES
    }
    flipped = [name for status in sorted(DETECTED_STATUSES) for name in outcomes.get(status, [])]
    simulated["survived"] = sorted([*simulated.get("survived", []), *flipped])
    return owner_report(owner, simulated)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--floors", type=Path, default=DEFAULT_FLOORS)
    parser.add_argument("--max-children", type=int, default=2)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--owner",
        action="append",
        default=[],
        help="Restrict the run to this floors.json owner key (repeatable).",
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
        "--update-floors",
        action="store_true",
        help="Record measured rates and raise (never lower) each floor.",
    )
    parser.add_argument(
        "--negative-control",
        metavar="OWNER",
        help="Prove the comparator fails when OWNER loses every kill; exit 0 if detected.",
    )
    parser.add_argument(
        "--check-floor-ratchet",
        action="store_true",
        help="Validate floors.json and reject floor decreases against --base-ref.",
    )
    parser.add_argument("--base-ref", help="Git ref holding the previous floors.json.")
    return parser.parse_args(argv)


def _select_owners(owners: tuple[Owner, ...], keys: list[str]) -> tuple[Owner, ...]:
    if not keys:
        return owners
    known = {owner.key: owner for owner in owners}
    unknown = sorted(set(keys) - set(known))
    if unknown:
        raise PilotError(f"unknown mutation owner(s): {', '.join(unknown)}")
    return tuple(known[key] for key in sorted(set(keys)))


def _run_ratchet(args: argparse.Namespace) -> int:
    current = load_baseline(args.floors, label="mutation floors")
    base = None
    if args.base_ref:
        base = load_baseline_at_ref(REPO_ROOT, args.floors, args.base_ref, label="mutation floors")
    problems = check_floor_ratchet(current, base)
    for problem in problems:
        print(f"[x] Mutation floor ratchet: {_ascii(problem)}", file=sys.stderr)
    if problems:
        return 1
    print(f"[+] Mutation floors valid; no floor lowered ({len(current['owners'])} owners).")
    return 0


def _run_negative_control(owners: tuple[Owner, ...], key: str) -> int:
    owner = _select_owners(owners, [key])[0]
    report = negative_control(owner, _outcomes(_load_exit_codes(owner, REPO_ROOT)))
    rate = report["measured"]["kill_rate"]
    if report["status"] == "regression":
        print(
            f"[+] Negative control detected: {key} kill rate {rate} "
            f"is below floor {owner.floor}; the floor check fails as designed."
        )
        return 0
    print(
        f"[x] Negative control NOT detected: {key} kill rate {rate} vs floor {owner.floor}",
        file=sys.stderr,
    )
    return 1


def _print_summary(reports: dict[str, dict[str, Any]], elapsed: float | None) -> None:
    total = sum(r["measured"]["total"] for r in reports.values())
    runtime = "metadata only" if elapsed is None else f"{elapsed:.1f}s runtime"
    print(f"[i] Mutation floors: {len(reports)} owners, {total} mutants, {runtime}")
    for key, report in reports.items():
        measured = report["measured"]
        symbol = "[+]" if report["status"] == "accepted" else "[x]"
        print(
            f"{symbol} {key}: kill_rate={measured['kill_rate']} floor={report['floor']} "
            f"total={measured['total']} killed={measured['killed']} "
            f"timeout={measured['timeout']} survived={measured['survived']} "
            f"no_tests={measured['no_tests']} equivalent={measured['equivalent']}"
        )
        for label in ("unlisted_survivors", "killed_equivalents", "stale_equivalents"):
            if report[label]:
                print(f"[!] {key}: {len(report[label])} {label.replace('_', ' ')}")


def _run_floors(
    args: argparse.Namespace, payload: dict[str, Any], owners: tuple[Owner, ...]
) -> int:
    selected = _select_owners(owners, args.owner)
    elapsed = None
    if args.report_only:
        print("[!] Report-only mode reuses existing mutmut metadata without executing tests.")
    else:
        print(f"[i] Starting {'cached' if args.reuse_cache else 'fresh'} mutation execution.")
        elapsed = _run_mutmut(
            selected,
            max_children=args.max_children,
            reuse_cache=args.reuse_cache,
            repo_root=REPO_ROOT,
        )
    reports = {
        owner.key: owner_report(owner, _outcomes(_load_exit_codes(owner, REPO_ROOT)))
        for owner in selected
    }
    if args.update_floors:
        payload = updated_floors_payload(payload, reports)
        write_baseline(args.floors, payload, label="mutation floors")
        parse_floors(payload)
    failed = any(report["status"] == "regression" for report in reports.values())
    _write_report(
        args.output,
        {
            "kill_rate_definition": KILL_RATE_DEFINITION,
            "owners": reports,
            "schema_version": SCHEMA_VERSION,
            "status": "regression" if failed else "accepted",
            "tool": {"name": "mutmut", "version": MUTMUT_VERSION},
        },
    )
    _print_summary(reports, elapsed)
    print(f"[i] Report written to {_ascii(str(args.output))}")
    if failed:
        print(
            "[x] A kill rate dropped below its floor or mutation evidence is incomplete. "
            "Run 'mutmut show <name>' for each unlisted survivor and add a behavioral "
            "test; list a survivor as equivalent only with a reviewed reason.",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the mutation floors check, a negative control, or the floor ratchet."""
    args = _parse_args(argv)
    if args.max_children < 1:
        print("[x] --max-children must be at least 1", file=sys.stderr)
        return 2
    try:
        if args.check_floor_ratchet:
            return _run_ratchet(args)
        payload = load_baseline(args.floors, label="mutation floors")
        owners = parse_floors(payload)
        if args.negative_control:
            return _run_negative_control(owners, args.negative_control)
        return _run_floors(args, payload, owners)
    except (BaselineError, PilotError) as error:
        print(f"[x] Mutation floors failed: {_ascii(str(error))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
