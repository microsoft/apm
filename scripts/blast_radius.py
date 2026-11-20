"""Blast-radius PR slice: nightly coverage map and fail-closed PR plan (#4117).

Two halves share this module:

* ``build-map`` turns the nightly ``--cov-context=test`` coverage data into the
  ``blast-radius-map`` artifact: ``{src path: [test node ids]}``. Subprocess
  coverage (the CLI under test) is attributed to the running test through
  the ``.pth`` hook installed by ``install-subprocess-hook`` and the
  ``APM_BLAST_RADIUS_CONTEXT`` variable this module's pytest hook exports.
* ``plan`` decides what one pull request runs. :func:`decide` is the single
  authority: it runs the FULL integration suite whenever the map cannot be
  trusted (missing, malformed, wrong schema, stale or future-dated), when the
  harness changed, when a changed ``src/`` path has no owner ``modules``
  glob, or when a ``src/`` change selects nothing. Otherwise it selects the
  map's tests for the changed sources, the owners' ``families`` and changed
  integration test files. Lifecycle smoke is the floor: it runs in its own
  required job, so the slice executes its selection minus smoke.

Run ``python scripts/blast_radius.py --help`` for the CLI.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import subprocess
import sys
import sysconfig
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCHEMA_VERSION = 1
MAX_MAP_AGE = timedelta(hours=48)
MAX_CLOCK_SKEW = timedelta(minutes=10)
MAX_SHARDS = 8
CONTEXT_ENV = "APM_BLAST_RADIUS_CONTEXT"
SMOKE_MARK_EXPR = "lifecycle_smoke and not lifecycle_merge_group"
LANE_MARK_EXPR = (
    "not live and not benchmark and not requires_github_token and not requires_network_integration"
)
SLICE_MARK_EXPR = f"({LANE_MARK_EXPR}) and not ({SMOKE_MARK_EXPR})"
INTEGRATION_ROOT = "tests/integration/"
SRC_ROOT = "src/"

# A change to any of these can alter how every test runs or is selected.
HARNESS_PATTERNS: tuple[str, ...] = (
    "tests/conftest.py",
    "tests/integration/conftest.py",
    "tests/utils/*",
    "pyproject.toml",
    "uv.lock",
    "scripts/blast_radius.py",
    "scripts/ci_sharding.py",
    "scripts/ci_*.py",
    ".github/workflows/*",
    ".github/ci-durations/*",
)

MODE_FULL = "full"
MODE_SLICE = "slice"
MODE_NOOP = "noop"

REASON_MAP_INVALID = "map-missing-or-malformed"
REASON_MAP_STALE = "map-stale-or-future"
REASON_HARNESS = "harness-changed"
REASON_UNOWNED = "unowned-src-change"
REASON_EMPTY = "empty-slice-with-src-change"
REASON_SELECTED = "selected"
REASON_NOTHING_TESTABLE = "no-src-tests-or-harness-change"

_MAP_FIELDS = frozenset(
    {"schema_version", "generated_at", "commit", "run_id", "mark_expr", "test_count", "files"}
)


@dataclass(frozen=True)
class Change:
    """One ``git diff --name-status -M`` row."""

    status: str
    path: str
    old_path: str | None = None

    @property
    def paths(self) -> tuple[str, ...]:
        """Paths this change touches; a deletion keeps its old path."""
        return (self.old_path, self.path) if self.old_path else (self.path,)


@dataclass(frozen=True)
class OwnerOverlay:
    """The blast-radius fields of one canonical owner."""

    owner_id: str
    modules: tuple[str, ...]
    families: tuple[str, ...]


@dataclass(frozen=True)
class CoverageMap:
    """A validated ``blast-radius-map`` artifact."""

    generated_at: datetime
    commit: str
    files: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class Decision:
    """What one pull request runs, and why."""

    mode: str
    reason: str
    detail: tuple[str, ...] = ()
    targets: tuple[str, ...] = ()
    owners: tuple[str, ...] = field(default=())


def parse_name_status(text: str) -> tuple[Change, ...]:
    """Parse ``git diff --name-status -M`` output."""
    changes: list[Change] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        if status[:1] in {"R", "C"} and len(parts) == 3:
            changes.append(Change(status[:1], parts[2], parts[1]))
        elif len(parts) == 2:
            changes.append(Change(status[:1], parts[1]))
        else:
            raise ValueError(f"malformed name-status line: {line!r}")
    return tuple(changes)


def load_map(path: Path, now: datetime) -> tuple[CoverageMap | None, str | None]:
    """Return the validated map, or the fail-closed reason it cannot be used."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, REASON_MAP_INVALID
    if not isinstance(document, dict) or set(document) != _MAP_FIELDS:
        return None, REASON_MAP_INVALID
    files = document["files"]
    if (
        document["schema_version"] != SCHEMA_VERSION
        or not isinstance(document["commit"], str)
        or not isinstance(document["mark_expr"], str)
        or type(document["test_count"]) is not int
        or not isinstance(files, dict)
        or not all(
            isinstance(key, str)
            and key.startswith(SRC_ROOT)
            and isinstance(nodes, list)
            and all(isinstance(node, str) and "::" in node for node in nodes)
            for key, nodes in files.items()
        )
    ):
        return None, REASON_MAP_INVALID
    try:
        generated_at = datetime.fromisoformat(str(document["generated_at"]))
    except ValueError:
        return None, REASON_MAP_INVALID
    if generated_at.tzinfo is None:
        return None, REASON_MAP_INVALID
    if generated_at > now + MAX_CLOCK_SKEW or now - generated_at > MAX_MAP_AGE:
        return None, REASON_MAP_STALE
    return (
        CoverageMap(
            generated_at=generated_at,
            commit=document["commit"],
            files={key: tuple(nodes) for key, nodes in files.items()},
        ),
        None,
    )


def harness_changes(changes: Iterable[Change]) -> tuple[str, ...]:
    """Return changed paths that belong to the test or CI harness."""
    return tuple(
        sorted(
            {
                path
                for change in changes
                for path in change.paths
                if any(fnmatch.fnmatchcase(path, pattern) for pattern in HARNESS_PATTERNS)
            }
        )
    )


def nothing_testable_changed(changes: Iterable[Change]) -> bool:
    """Scope amendment (pending): no src/, tests/ or harness path changed.

    Flip to ``return False`` to restore the literal rule (an empty slice
    always runs the full suite).
    """
    return not any(
        path.startswith((SRC_ROOT, "tests/")) for change in changes for path in change.paths
    ) and not harness_changes(changes)


def _owners_for(path: str, owners: Sequence[OwnerOverlay]) -> tuple[OwnerOverlay, ...]:
    return tuple(
        owner
        for owner in owners
        if any(fnmatch.fnmatchcase(path, module) for module in owner.modules)
    )


def _full(reason: str, detail: Iterable[str] = ()) -> Decision:
    return Decision(MODE_FULL, reason, tuple(sorted(detail)), (INTEGRATION_ROOT.rstrip("/"),))


def decide(
    changes: Sequence[Change],
    coverage_map: CoverageMap | None,
    map_failure: str | None,
    owners: Sequence[OwnerOverlay],
    integration_files: Sequence[str],
) -> Decision:
    """The one fail-closed authority for what a pull request runs."""
    if nothing_testable_changed(changes):
        return Decision(MODE_NOOP, REASON_NOTHING_TESTABLE)
    harness = harness_changes(changes)
    if harness:
        return _full(REASON_HARNESS, harness)
    if map_failure == REASON_MAP_STALE:
        return _full(REASON_MAP_STALE)
    if coverage_map is None:
        return _full(REASON_MAP_INVALID)

    src_paths = sorted({p for c in changes for p in c.paths if p.startswith(SRC_ROOT)})
    owned = {path: _owners_for(path, owners) for path in src_paths}
    unowned = [path for path, matched in owned.items() if not matched]
    if unowned:
        return _full(REASON_UNOWNED, unowned)

    targets: set[str] = set()
    for path in src_paths:
        targets.update(coverage_map.files.get(path, ()))
    matched_owners = {owner for matched in owned.values() for owner in matched}
    for owner in matched_owners:
        targets.update(
            test
            for test in integration_files
            if any(fnmatch.fnmatchcase(test, family) for family in owner.families)
        )
    if src_paths and not targets:
        return _full(REASON_EMPTY, src_paths)
    targets.update(
        change.path
        for change in changes
        if change.status != "D"
        and change.path.startswith(INTEGRATION_ROOT)
        and change.path in integration_files
    )
    return Decision(
        MODE_SLICE,
        REASON_SELECTED,
        tuple(src_paths),
        tuple(sorted(targets)),
        tuple(sorted(owner.owner_id for owner in matched_owners)),
    )


def load_owner_overlays(root: Path) -> tuple[OwnerOverlay, ...]:
    """Read owner ``modules``/``families`` through the canonical registry loader.

    Any load failure yields no owners, so every src change fails closed.
    """
    sys.path.insert(0, str(root))
    try:
        from scripts.architecture_linter.inventory import build_inventory
        from scripts.architecture_linter.registry import load_registry

        registry = load_registry(root / ".apm/architecture/owners", build_inventory(root).files)
    except Exception:
        return ()
    return tuple(
        OwnerOverlay(
            owner.id,
            tuple(getattr(owner, "modules", ())),
            tuple(getattr(owner, "families", ())),
        )
        for owner in registry.owners
        if getattr(owner, "modules", ())
    )


def integration_test_files(root: Path) -> tuple[str, ...]:
    return tuple(
        sorted(
            path.relative_to(root).as_posix()
            for path in (root / INTEGRATION_ROOT).rglob("test_*.py")
        )
    )


def _collect(root: Path, targets: Sequence[str], mark_expr: str, args_file: Path) -> int:
    """Write `targets` to `args_file`, collect them under `mark_expr`, count nodes."""
    args_file.parent.mkdir(parents=True, exist_ok=True)
    args_file.write_text("\n".join(targets) + "\n", encoding="utf-8")
    command = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--collect-only", "-q"]
    command += ["-m", mark_expr, f"@{args_file.resolve()}"]
    completed = subprocess.run(  # noqa: S603 - fixed argv of the running interpreter
        command, cwd=root, capture_output=True, text=True, check=False
    )
    if completed.returncode not in (0, 5):
        raise RuntimeError(
            f"collection failed ({completed.returncode}): {completed.stdout[-2000:]}"
        )
    return sum(1 for line in completed.stdout.splitlines() if "::" in line)


def plan_command(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    now = datetime.now(UTC)
    changes = parse_name_status(Path(args.changes).read_text(encoding="utf-8"))
    coverage_map, failure = load_map(Path(args.map), now)
    decision = decide(
        changes,
        coverage_map,
        failure,
        load_owner_overlays(root),
        integration_test_files(root),
    )
    out = Path(args.out)
    executed = smoke = 0
    if decision.mode != MODE_NOOP:
        executed = _collect(root, decision.targets, SLICE_MARK_EXPR, out.with_suffix(".args"))
        smoke_args = out.with_name(out.stem + "-smoke.args")
        smoke = _collect(root, (INTEGRATION_ROOT.rstrip("/"),), SMOKE_MARK_EXPR, smoke_args)
    shard_count = min(MAX_SHARDS, max(1, -(-executed // 150))) if executed else 0
    record = {
        "mode": decision.mode,
        "reason": decision.reason,
        "detail": list(decision.detail),
        "owners": list(decision.owners),
        "targets": list(decision.targets),
        "executed_nodes": executed,
        "smoke_nodes": smoke,
        "selected_nodes": executed + smoke,
        "shard_count": shard_count,
        "mark_expr": SLICE_MARK_EXPR,
        "map_commit": coverage_map.commit if coverage_map else None,
        "map_generated_at": coverage_map.generated_at.isoformat() if coverage_map else None,
    }
    out.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as handle:
            handle.write(f"mode={decision.mode}\n")
            handle.write(f"shard_count={shard_count}\n")
            handle.write(f"shards={json.dumps(list(range(1, shard_count + 1)))}\n")
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as handle:
            handle.write(
                f"### Blast radius: {decision.mode} ({decision.reason})\n\n"
                f"- executed nodes: {executed}\n- lifecycle smoke floor: {smoke}\n"
                f"- shards: {shard_count}\n- owners: {', '.join(decision.owners) or '-'}\n"
                f"- detail: {', '.join(decision.detail[:20]) or '-'}\n"
            )
    print(json.dumps({k: record[k] for k in ("mode", "reason", "executed_nodes", "smoke_nodes")}))
    return 0


def _node_id(context: str) -> str | None:
    node = context.rsplit("|", 1)[0] if "|" in context else context
    return node if "::" in node else None


def build_map(data_file: Path, root: Path) -> dict[str, list[str]]:
    """Return ``{src path: sorted test node ids}`` from coverage contexts."""
    import coverage

    data = coverage.CoverageData(basename=str(data_file))
    data.read()
    files: dict[str, set[str]] = {}
    resolved_root = root.resolve()
    for measured in data.measured_files():
        path = Path(measured)
        if path.is_absolute():
            try:
                path = path.resolve().relative_to(resolved_root)
            except ValueError:
                continue
        relative = path.as_posix()
        if not relative.startswith(SRC_ROOT):
            continue
        contexts = data.contexts_by_lineno(measured)
        nodes = {node for values in contexts.values() for c in values if (node := _node_id(c))}
        if nodes:
            files.setdefault(relative, set()).update(nodes)
    return {path: sorted(nodes) for path, nodes in sorted(files.items())}


def build_map_command(args: argparse.Namespace) -> int:
    files = build_map(Path(args.data), Path(args.root))
    tests = {node for nodes in files.values() for node in nodes}
    document = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": args.commit,
        "run_id": args.run_id,
        "mark_expr": args.mark_expr,
        "test_count": len(tests),
        "files": files,
    }
    Path(args.out).write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", "utf-8")
    print(f"blast-radius map: {len(files)} src files, {len(tests)} tests")
    if not files:
        print("::error::blast-radius map is empty; subprocess coverage did not attribute tests")
        return 1
    return 0


_HOOK = (
    "import os, coverage; _c = coverage.process_startup(); "
    f"_x = os.environ.get('{CONTEXT_ENV}'); _c and _x and _c.switch_context(_x)\n"
)


def install_subprocess_hook_command(_args: argparse.Namespace) -> int:
    target = Path(sysconfig.get_paths()["purelib"]) / "apm_blast_radius_coverage.pth"
    target.write_text(_HOOK, encoding="ascii")
    print(f"installed {target}")
    return 0


def pytest_runtest_setup(item: object) -> None:
    """Export the running test id so CLI subprocesses attribute coverage to it."""
    os.environ[CONTEXT_ENV] = getattr(item, "nodeid", "")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="blast_radius", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="decide the PR slice (fails closed to full)")
    plan.add_argument("--root", default=".")
    plan.add_argument("--map", required=True)
    plan.add_argument("--changes", required=True, help="git diff --name-status -M output")
    plan.add_argument("--out", required=True)
    plan.add_argument("--github-output")
    plan.add_argument("--summary")
    plan.set_defaults(func=plan_command)
    build = sub.add_parser("build-map", help="turn per-test coverage into the map artifact")
    build.add_argument("--root", default=".")
    build.add_argument("--data", required=True)
    build.add_argument("--commit", required=True)
    build.add_argument("--run-id", required=True)
    build.add_argument("--mark-expr", required=True)
    build.add_argument("--out", required=True)
    build.set_defaults(func=build_map_command)
    hook = sub.add_parser("install-subprocess-hook", help="enable subprocess coverage")
    hook.set_defaults(func=install_subprocess_hook_command)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
