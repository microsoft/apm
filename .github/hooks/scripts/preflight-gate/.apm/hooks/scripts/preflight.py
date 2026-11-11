#!/usr/bin/env python3
"""preflight.py -- diff-aware local CI mirror that writes a gate receipt.

Usage (from anywhere inside the repo):
    python3 .github/hooks/scripts/preflight-gate/.apm/hooks/scripts/preflight.py
        [--base REF] [--tiers auto|T0,T1,..] [--plan] [--json]
        [--keep-going] [--no-fetch] [--jobs N] [--print-ci-fingerprints]

Exit codes: 0 pass, 1 fail or partial, 2 usage or internal error.
Stdlib only. Unix/WSL only (native Windows is refused: CI covers Windows).
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import platform  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
from collections.abc import Callable  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402
from pathlib import Path  # noqa: E402
from types import ModuleType  # noqa: E402

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import preflight_lib as lib  # noqa: E402
import preflight_policy as default_policy  # noqa: E402

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
CHECK_TIMEOUT_S = 3600
GGREP_PRELUDE = 'grep() { command ggrep "$@"; }\n'


class UsageError(Exception):
    """Bad invocation or unusable repository state (exit 2)."""


def _say(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _is_windows() -> bool:
    return os.name == "nt"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="preflight.py",
        description="Run the diff-aware preflight and write the gate receipt.",
    )
    parser.add_argument("--base", default="origin/main", help="base ref (default origin/main)")
    parser.add_argument("--tiers", default="auto", help="auto (default) or e.g. T0,T1")
    parser.add_argument("--plan", action="store_true", help="print selected tiers and exit")
    parser.add_argument("--json", action="store_true", help="print the receipt JSON on stdout")
    parser.add_argument("--keep-going", action="store_true", help="run tiers after a failure")
    parser.add_argument("--no-fetch", action="store_true", help="do not fetch the base ref")
    parser.add_argument(
        "--jobs",
        type=int,
        default=0,
        help="concurrent checks per tier (default min(4, cpus); 1 = serial)",
    )
    parser.add_argument(
        "--print-ci-fingerprints",
        action="store_true",
        help="print current fingerprints of mirrored CI jobs and exit",
    )
    return parser.parse_args(argv)


def parse_tiers(value: str, known: tuple[str, ...]) -> list[str] | None:
    """None for auto, else the ordered explicit tier list."""
    if value.strip().lower() == "auto":
        return None
    tiers = [t.strip().upper() for t in value.split(",") if t.strip()]
    unknown = [t for t in tiers if t not in known]
    if not tiers or unknown:
        raise UsageError(f"--tiers must be 'auto' or a subset of {','.join(known)}")
    return [t for t in known if t in tiers]


def fetch_base(root: Path, base_ref: str, remote_names: list[str]) -> str | None:
    """Fetch `<remote> <branch>` for a remote-tracking base. Returns a warning."""
    for remote in remote_names:
        if base_ref.startswith(remote + "/"):
            branch = base_ref[len(remote) + 1 :]
            try:
                proc = lib.git(["fetch", "--quiet", "--no-tags", remote, branch], root, timeout=180)
            except (OSError, subprocess.SubprocessError) as exc:
                return f"git fetch {remote} {branch} failed: {exc}"
            if proc.returncode != 0:
                return f"git fetch {remote} {branch} failed: {proc.stderr.strip()[:200]}"
            return None
    return None


def resolve_base(root: Path, base_ref: str) -> tuple[str, str, list[str]]:
    """Return (base_ref, base_sha, warnings); fall back to `main`."""
    warnings: list[str] = []
    sha = lib.rev_commit(base_ref, root)
    if sha is None and base_ref != "main":
        fallback = lib.rev_commit("main", root)
        if fallback is not None:
            warnings.append(f"base {base_ref!r} not found; fell back to 'main'")
            return "main", fallback, warnings
    if sha is None:
        raise UsageError(f"cannot resolve base {base_ref!r} (nor 'main'); pass --base")
    return base_ref, sha, warnings


def shell_prelude(checks: list, warnings: list[str]) -> tuple[str, str | None]:
    """Adapt hosts without GNU grep -P. Returns (prelude, fatal problem)."""
    if not any(getattr(c, "needs_pcre_grep", False) for c in checks):
        return "", None
    if lib.host_has_pcre_grep():
        return "", None
    if shutil.which("ggrep"):
        warnings.append("host grep lacks -P; grep -P steps ran through ggrep")
        return GGREP_PRELUDE, None
    return "", "host grep lacks -P and ggrep is missing (macOS: brew install grep)"


def _run_command(check, root: Path, log_path: Path, env: dict[str, str], prelude: str) -> int:
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"# check: {check.id} (tier {check.tier})\n# cmd:\n{check.cmd}\n# ---\n")
        log.flush()
        try:
            proc = subprocess.run(  # noqa: S603 - policy-owned command via bash
                ["bash", "--noprofile", "--norc", "-e", "-c", prelude + check.cmd],  # noqa: S607
                cwd=str(root),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=CHECK_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            log.write(f"\n# TIMEOUT after {CHECK_TIMEOUT_S}s\n")
            return 124
        except OSError as exc:
            log.write(f"\n# could not start bash: {exc}\n")
            return 127
        return proc.returncode


def _internal(check, ctx, policy: ModuleType) -> tuple[bool, list[str]]:
    root = ctx.root
    if check.internal == "bundle_stale":
        deployed = root / lib.DEPLOYED_SCRIPTS_REL
        if not deployed.is_dir():
            return False, [f"{lib.DEPLOYED_SCRIPTS_REL} missing: run `apm install`"]
        diffs = lib.bundle_differences(root / lib.SOURCE_SCRIPTS_REL, deployed)
        if diffs:
            return False, [*diffs, "deployed preflight bundle is stale: run `apm install`"]
        return True, ["deployed bundle matches packages/preflight-gate"]
    if check.internal == "ci_drift":
        current = lib.ci_fingerprints(root, policy.MIRRORED_CI_JOBS)
        lines = []
        for key, expected in policy.CI_FINGERPRINTS.items():
            if current.get(key) != expected:
                lines.append(f"{key}: expected {expected} got {current.get(key)}")
        if lines:
            lines.append(
                "mirrored CI job changed: update preflight_policy.py commands and "
                "CI_FINGERPRINTS (see --print-ci-fingerprints)"
            )
            return False, lines
        return True, [f"{len(current)} mirrored CI job(s) unchanged"]
    if check.internal == "ascii_guard":
        return lib.ascii_guard(root, ctx.merge_base, list(policy.ASCII_GUARD_PATHS))
    if check.internal == "uvlock_guard":
        return lib.uvlock_guard(root, ctx.merge_base, tuple(policy.UVLOCK_ALLOWED_HOSTS))
    raise ValueError(f"unknown internal check {check.internal!r}")


def _run_internal(check, ctx, policy: ModuleType, log_path: Path) -> int:
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"# check: {check.id} (tier {check.tier})\n# internal: {check.internal}\n# ---\n")
        try:
            ok, lines = _internal(check, ctx, policy)
        except Exception as exc:
            log.write(f"internal check crashed: {exc!r}\n")
            return 2
        for line in lines:
            log.write(line + "\n")
    return 0 if ok else 1


def default_jobs() -> int:
    return max(1, min(4, os.cpu_count() or 1))


Outcome = tuple[int, float, Path]


def run_tier(checks: list, execute: Callable[[object], Outcome], jobs: int) -> list[Outcome]:
    """Run one tier; return outcomes in policy order.

    Non-exclusive checks run concurrently on a bounded pool (siblings always
    finish even when one fails). Exclusive checks (xdist / E2E suites that
    saturate the host) then run one at a time, never beside anything else.
    """
    outcomes: dict[int, Outcome] = {}
    shared = [i for i, c in enumerate(checks) if not getattr(c, "exclusive", False)]
    if jobs > 1 and len(shared) > 1:
        with ThreadPoolExecutor(max_workers=min(jobs, len(shared))) as pool:
            futures = {i: pool.submit(execute, checks[i]) for i in shared}
            for i, future in futures.items():
                outcomes[i] = future.result()
    else:
        for i in shared:
            outcomes[i] = execute(checks[i])
    for i, check in enumerate(checks):
        if i not in outcomes:
            outcomes[i] = execute(check)
    return [outcomes[i] for i in range(len(checks))]


def _plan_report(selected: dict[str, str], plan: dict[str, list]) -> None:
    for tier, reason in selected.items():
        _say(f"[i] {tier}: {reason}")
        for check in plan.get(tier, []):
            _say(f"      - {check.id}")


def _check_record(check, exit_code: int, duration: float, log: Path) -> dict:
    return {
        "id": check.id,
        "tier": check.tier,
        "cmd": check.cmd if check.cmd is not None else f"internal:{check.internal}",
        "exit": exit_code,
        "duration_s": round(duration, 2),
        "log": str(log),
    }


def run(
    argv: list[str],
    policy: ModuleType = default_policy,
    cwd: str | Path | None = None,
) -> int:
    """Entry point with injectable policy and cwd (tests)."""
    args = parse_args(argv)
    if _is_windows():
        _say("[x] preflight runs on Unix/WSL only; Windows lanes stay in CI.")
        return EXIT_USAGE
    info = lib.repo_info(cwd or os.getcwd())
    if info is None:
        raise UsageError("not inside a git work tree")
    root = Path(info["toplevel"])
    git_dir = Path(info["git_dir"])

    if args.print_ci_fingerprints:
        print(json.dumps(lib.ci_fingerprints(root, policy.MIRRORED_CI_JOBS), indent=2))
        return EXIT_PASS

    explicit = parse_tiers(args.tiers, tuple(policy.TIERS))
    started_at = lib.now_iso()
    warnings: list[str] = []
    remote_names = lib.remotes(root)
    fetched = False
    if not args.no_fetch:
        problem = fetch_base(root, args.base, remote_names)
        if problem:
            warnings.append(problem)
        else:
            fetched = any(args.base.startswith(r + "/") for r in remote_names)
    base_ref, base_sha, base_warnings = resolve_base(root, args.base)
    warnings += base_warnings
    head, head_tree = lib.head_and_tree(root)
    if head is None:
        raise UsageError("HEAD does not resolve to a commit")
    merge_base = lib.git_out(["merge-base", base_sha, head], root)
    if not merge_base:
        raise UsageError(f"no merge-base between {base_ref} and HEAD")

    changed = lib.changed_files(root, merge_base)
    required = policy.select(changed)
    to_run = explicit if explicit is not None else list(required)
    selected = {t: required.get(t, "requested via --tiers") for t in to_run}
    ctx = policy.Context(
        root=root,
        base_ref=base_ref,
        base_sha=base_sha,
        merge_base=merge_base,
        changed=changed,
        selected=selected,
    )
    plan = {tier: policy.build_checks(tier, ctx) for tier in to_run}

    _say(f"[i] base {base_ref} @ {base_sha[:12]}, merge-base {merge_base[:12]}")
    _say(f"[i] {len(changed)} changed file(s); required tiers: {', '.join(required)}")
    if args.plan:
        _plan_report(selected, plan)
        if args.json:
            out = {
                "required_tiers": required,
                "planned": {t: [c.id for c in cs] for t, cs in plan.items()},
                "changed_files": changed,
            }
            print(json.dumps(out, indent=2))
        return EXIT_PASS

    clean_start = lib.is_clean(root)
    tree0 = lib.worktree_tree(root, git_dir)
    logs_dir = lib.preflight_dir(git_dir) / "logs"
    if logs_dir.is_dir():
        for old in logs_dir.glob("*.log"):
            old.unlink()
    logs_dir.mkdir(parents=True, exist_ok=True)

    all_checks = [c for tier in to_run for c in plan[tier]]
    prelude, pcre_problem = shell_prelude(all_checks, warnings)
    env = dict(os.environ)
    env.update(
        {
            "PREFLIGHT_ROOT": str(root),
            "PREFLIGHT_BASE_REF": base_ref,
            "PREFLIGHT_BASE_SHA": base_sha,
            "PREFLIGHT_MERGE_BASE": merge_base,
        }
    )

    jobs = args.jobs if args.jobs > 0 else default_jobs()

    def execute(check) -> Outcome:
        log_path = logs_dir / f"{check.id}.log"
        began = time.monotonic()
        try:
            if check.cmd is None:
                code = _run_internal(check, ctx, policy, log_path)
            elif check.needs_pcre_grep and pcre_problem:
                log_path.write_text(f"# check: {check.id}\n{pcre_problem}\n", encoding="utf-8")
                code = 127
            else:
                check_env = dict(env)
                check_env.update(check.env)
                code = _run_command(check, root, log_path, check_env, prelude)
        except Exception as exc:
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"\n# runner error: {exc!r}\n")
            code = 2
        return code, time.monotonic() - began, log_path

    records: list[dict] = []
    ran: list[str] = []
    tier_wall: dict[str, float] = {}
    for tier in to_run:
        _say(f"[*] {tier}: {selected[tier]}")
        began = time.monotonic()
        outcomes = run_tier(plan[tier], execute, jobs)
        tier_wall[tier] = round(time.monotonic() - began, 2)
        tier_failed = False
        for check, (code, duration, log_path) in zip(plan[tier], outcomes, strict=True):
            records.append(_check_record(check, code, duration, log_path))
            if code == 0:
                _say(f"  [+] {check.id} ({duration:.1f}s)")
            else:
                tier_failed = True
                _say(f"  [x] {check.id} (exit {code}) log: {log_path}")
        _say(f"  [i] {tier} wall time {tier_wall[tier]:.1f}s ({jobs} job(s))")
        ran.append(tier)
        if tier_failed and not args.keep_going:
            break

    tree1 = lib.worktree_tree(root, git_dir)
    head_end, _ = lib.head_and_tree(root)
    clean_end = lib.is_clean(root)
    hygiene_log = logs_dir / "hygiene.log"
    hygiene_log.write_text(
        f"tree0 {tree0}\ntree1 {tree1}\nhead0 {head}\nhead1 {head_end}\n", encoding="utf-8"
    )
    if tree1 != tree0:
        records.append(
            {
                "id": "hygiene.tree-mutated",
                "tier": "post",
                "cmd": "internal:tree-compare",
                "exit": 1,
                "duration_s": 0.0,
                "log": str(hygiene_log),
            }
        )
        _say("  [x] hygiene.tree-mutated: a check modified the working tree")
    if head_end != head:
        records.append(
            {
                "id": "hygiene.head-moved",
                "tier": "post",
                "cmd": "internal:head-compare",
                "exit": 1,
                "duration_s": 0.0,
                "log": str(hygiene_log),
            }
        )
        _say("  [x] hygiene.head-moved: HEAD changed during the run")

    failed = [r for r in records if r["exit"] != 0]
    if failed:
        status = "fail"
    elif not set(required) <= set(ran):
        status = "partial"
    else:
        status = "pass"

    ineligible: list[str] = []
    if explicit is not None:
        ineligible.append("--tiers was not auto")
    if not clean_start:
        ineligible.append("worktree was dirty at start (commit first)")
    if not clean_end:
        ineligible.append("worktree was dirty at end")
    if head_tree != tree0 and clean_start:
        ineligible.append("working tree did not match HEAD^{tree}")

    receipt = {
        "schema": lib.SCHEMA,
        "status": status,
        "gate_eligible": not ineligible,
        "ineligible_reasons": ineligible,
        "tree": tree0,
        "head": head,
        "base_ref": base_ref,
        "base_branch": lib.base_branch_name(base_ref, remote_names),
        "base_sha": base_sha,
        "merge_base": merge_base,
        "fetched": fetched,
        "bundle_hash": lib.deployed_bundle_hash(root),
        "required_tiers": required,
        "ran_tiers": ran,
        "parallel": jobs > 1,
        "jobs": jobs,
        "tier_wall_s": tier_wall,
        "checks": records,
        "warnings": warnings,
        "ci_only_lanes": list(policy.CI_ONLY_LANES),
        "changed_files": len(changed),
        "host": {"os": platform.platform(), "python": platform.python_version()},
        "started_at": started_at,
        "finished_at": lib.now_iso(),
    }
    path = lib.receipt_path(git_dir)
    lib.write_json_atomic(path, receipt)

    for warning in warnings:
        _say(f"[!] {warning}")
    if status == "pass" and not ineligible:
        _say(f"[+] preflight PASS; receipt {path}")
    elif status == "pass":
        _say(f"[!] preflight passed but receipt is NOT gate-eligible: {'; '.join(ineligible)}")
    else:
        names = ", ".join(r["id"] for r in failed) or "required tiers not run"
        _say(f"[x] preflight {status.upper()}: {names}; receipt {path}")
    if args.json:
        print(json.dumps(receipt, indent=2, sort_keys=True))
    return EXIT_PASS if status == "pass" else EXIT_FAIL


def main(argv: list[str] | None = None) -> int:
    try:
        return run(sys.argv[1:] if argv is None else argv)
    except UsageError as exc:
        _say(f"[x] {exc}")
        return EXIT_USAGE
    except KeyboardInterrupt:
        _say("[x] interrupted")
        return EXIT_USAGE
    except Exception as exc:
        _say(f"[x] preflight internal error: {exc!r}")
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
