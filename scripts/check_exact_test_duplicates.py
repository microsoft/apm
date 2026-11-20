#!/usr/bin/env python3
"""Prevent growth in same-body and same-name duplicate tests.

Ratchet mode: the baseline records today's duplicates. A test that newly
joins a duplicate body group, or newly shares its ``Class::name`` with a
test in another file, fails. Baseline entries whose test no longer exists
or is no longer duplicated are reported as stale (info) and never fail, so
parallel consolidation changes do not conflict on the baseline.
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from ratchet_baseline import (
    BaselineError,
    load_baseline,
    validate_provisional,
    write_baseline,
)
from test_body_hash import ALGORITHM, body_digest, iter_test_functions
from test_equal_copies import CorpusIndex, equal_copy_groups
from test_file_inventory import tracked_python_paths

SCHEMA_VERSION = 2
SCOPE = ["tests/**/test_*.py", "tests/**/*_test.py"]
ALGORITHM_DOC = (
    "ast-body-v1: per collectable test function (test* functions and test* "
    "methods of Test* classes), SHA-256 of ast.dump(include_attributes=False) "
    "of the body with a leading docstring removed, first 16 hex characters. "
    "Literals and names are kept; comments, formatting and docstrings are "
    "ignored. Limitation: the hash ignores decorators (marks, parametrize "
    "values), signature and fixture definitions, class context (bases, "
    "setup, autouse fixtures) and module-level bindings, so equal hashes "
    "prove equal body text, not equal behavior. duplicate_bodies maps file -> "
    "tests whose body equals the body of at least one other test; "
    "duplicate_names maps file -> class-qualified tests whose Class::name "
    "also exists in another file. A test outside these lists that becomes a "
    "duplicate fails, except a baselined test moved to another file under the "
    "same name; entries may only be removed or re-keyed by such a move."
)
UPDATE_COMMAND = "uv run --frozen python scripts/check_exact_test_duplicates.py --update-baseline"
_REQUIRED_KEYS = {
    "algorithm",
    "algorithm_doc",
    "duplicate_bodies",
    "duplicate_names",
    "schema_version",
    "scope",
}


class _Invalid(ValueError):
    """Raised for a malformed duplicate baseline."""


def scan(root: Path, scope: list[str]) -> tuple[int, dict[str, str], dict[str, str]]:
    """Return module count, nodeid -> body digest and nodeid -> qualname."""
    modules = tracked_python_paths(root, scope=tuple(scope))
    digests: dict[str, str] = {}
    qualnames: dict[str, str] = {}
    for path in modules:
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for function in iter_test_functions(tree, relative):
            digests[function.nodeid] = body_digest(function)
            if function.classes:
                qualnames[function.nodeid] = function.qualname
    return len(modules), digests, qualnames


def duplicate_bodies(digests: dict[str, str]) -> dict[str, list[str]]:
    """Group node ids by body digest, keeping groups of two or more."""
    groups: dict[str, list[str]] = defaultdict(list)
    for nodeid, digest in digests.items():
        groups[digest].append(nodeid)
    return {digest: sorted(ids) for digest, ids in sorted(groups.items()) if len(ids) > 1}


def duplicate_names(qualnames: dict[str, str]) -> dict[str, list[str]]:
    """Group class-qualified node ids by ``Class::name`` across two or more files."""
    groups: dict[str, list[str]] = defaultdict(list)
    for nodeid, qualname in qualnames.items():
        groups[qualname].append(nodeid)
    return {
        qualname: sorted(ids)
        for qualname, ids in sorted(groups.items())
        if len({nodeid.split("::", 1)[0] for nodeid in ids}) > 1
    }


def _members(groups: dict[str, list[str]]) -> set[str]:
    return {nodeid for ids in groups.values() for nodeid in ids}


def baseline_layout(nodeids: set[str]) -> dict[str, list[str]]:
    """Return the per-file baseline layout (file -> sorted test names)."""
    layout: dict[str, list[str]] = defaultdict(list)
    for nodeid in sorted(nodeids):
        path, qualname = nodeid.split("::", 1)
        layout[path].append(qualname)
    return dict(layout)


def _load_layout(raw: object, key: str, *, qualified: bool) -> set[str]:
    if not isinstance(raw, dict) or list(raw) != sorted(raw):
        raise _Invalid(f"{key} must be an object with sorted file keys")
    nodeids: set[str] = set()
    for path, qualnames in raw.items():
        if (
            not isinstance(qualnames, list)
            or not qualnames
            or any(not isinstance(name, str) or not name for name in qualnames)
            or (qualified and any("::" not in name for name in qualnames))
            or qualnames != sorted(set(qualnames))
        ):
            raise _Invalid(f"{key}[{path!r}] must be a non-empty sorted unique list")
        nodeids.update(f"{path}::{name}" for name in qualnames)
    return nodeids


def _summarize(items: list[str], limit: int = 3) -> str:
    shown = ", ".join(items[:limit])
    return f"{shown} and {len(items) - limit} more" if len(items) > limit else shown


def _path(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


@dataclass(frozen=True)
class Comparison:
    """Outcome of one baseline comparison."""

    growth: list[str]
    stale: list[str]
    moved: dict[str, str]
    keep: dict[str, set[str]]


def _qualname(nodeid: str) -> str:
    return nodeid.split("::", 1)[1]


def _match_moves(new: set[str], stale: set[str]) -> dict[str, str]:
    """Pair new entries with stale entries of the same test name in another file."""
    pool: dict[str, list[str]] = defaultdict(list)
    for nodeid in sorted(stale):
        pool[_qualname(nodeid)].append(nodeid)
    moves: dict[str, str] = {}
    for nodeid in sorted(new):
        candidates = pool.get(_qualname(nodeid))
        if candidates:
            moves[nodeid] = candidates.pop(0)
    return moves


def compare(
    bodies: dict[str, list[str]],
    names: dict[str, list[str]],
    allowed_bodies: set[str],
    allowed_names: set[str],
    present: set[str],
) -> Comparison:
    """Return new duplicates, stale entries and moved (re-keyed) entries.

    A test that leaves a baselined file and reappears under the same name in
    another file is a move, not a new duplicate. Only baselined tests absent
    from the scanned corpus (``present``) can be the source of a move; a test
    that still exists but became unique is merely stale.
    """
    growth: list[str] = []
    stale: list[str] = []
    moved: dict[str, str] = {}
    keep: dict[str, set[str]] = {}
    for key, groups, allowed in (
        ("duplicate_bodies", bodies, allowed_bodies),
        ("duplicate_names", names, allowed_names),
    ):
        observed = _members(groups)
        moves = _match_moves(observed - allowed, allowed - present)
        moved.update({f"{key} {new}": f"{key} {old}" for new, old in moves.items()})
        stale += [f"{key} {nodeid}" for nodeid in sorted(allowed - observed - set(moves.values()))]
        keep[key] = (allowed & observed) | set(moves)
        accepted = allowed | set(moves)
        for group, ids in groups.items():
            twins = [nodeid for nodeid in ids if nodeid in accepted] or ids
            for nodeid in ids:
                if nodeid in accepted:
                    continue
                if key == "duplicate_bodies":
                    others = _summarize([other for other in twins if other != nodeid])
                    growth.append(
                        f"{_path(nodeid)}: test {_qualname(nodeid)} duplicates the body "
                        f"({ALGORITHM} {group}) of {others}. Fix: delete the copy, or "
                        "parametrize/differentiate it so it asserts a distinct contract; "
                        "do not add it to the baseline."
                    )
                else:
                    others = _summarize(
                        sorted({_path(other) for other in ids if _path(other) != _path(nodeid)})
                    )
                    growth.append(
                        f"{_path(nodeid)}: test {group} reuses a Class::name already defined "
                        f"in {others}. Fix: rename the class or test after the distinct "
                        "contract it checks, or delete the copy; do not add it to the "
                        "baseline."
                    )
    return Comparison(growth=growth, stale=stale, moved=moved, keep=keep)


def _validate(payload: dict[str, object]) -> tuple[set[str], set[str]]:
    if not set(payload) >= _REQUIRED_KEYS or not set(payload) <= _REQUIRED_KEYS | {"provisional"}:
        raise _Invalid("unexpected object shape")
    if payload["schema_version"] != SCHEMA_VERSION or payload["algorithm"] != ALGORITHM:
        raise _Invalid("unsupported schema or algorithm")
    if payload["algorithm_doc"] != ALGORITHM_DOC:
        raise _Invalid("algorithm_doc must match the checker's algorithm description")
    if payload["scope"] != SCOPE:
        raise _Invalid("scope must remain exact")
    return (
        _load_layout(payload["duplicate_bodies"], "duplicate_bodies", qualified=False),
        _load_layout(payload["duplicate_names"], "duplicate_names", qualified=True),
    )


def report_equal_copies(root: Path) -> int:
    """Print every D5 equal-copy group; never fails on findings."""
    try:
        paths = [
            path.relative_to(root).as_posix()
            for path in tracked_python_paths(root, scope=tuple(SCOPE))
        ]
        groups = equal_copy_groups(CorpusIndex(root), paths)
    except (OSError, UnicodeError, ValueError, SyntaxError) as error:
        print(f"[x] equal-copy scan failed: {error}", file=sys.stderr)
        return 2
    for keep, copies in groups:
        print(f"[i] equal copies of {keep}: {', '.join(copies)}")
    print(
        f"[i] equal-copy report (D5): {len(groups)} group(s), "
        f"{sum(len(copies) for _keep, copies in groups)} deletable copy(ies); report only"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Check the baseline, or prune stale entries when no growth is observed."""
    parser = argparse.ArgumentParser(
        description="Check per-test duplicate body and Class::name no-growth ceilings."
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument(
        "--report-equal-copies",
        action="store_true",
        help="Report-only: list D5 equal copies (deletable twins) and exit 0.",
    )
    parser.add_argument(
        "--allow-provisional",
        action="store_true",
        help="Allow explicitly provisional metadata during non-final checks.",
    )
    args = parser.parse_args(argv)

    root = args.root.resolve()
    baseline = (
        args.baseline.resolve()
        if args.baseline is not None
        else root / "tests" / "quality" / "exact_test_duplicates.json"
    )
    if args.report_equal_copies:
        return report_equal_copies(root)
    try:
        payload = load_baseline(baseline, label="test-duplicate")
        provisional = validate_provisional(
            payload,
            baseline,
            allow=args.allow_provisional,
            label="test-duplicate",
        )
        allowed_bodies, allowed_names = _validate(payload)
    except BaselineError as error:
        print(f"[x] {error}", file=sys.stderr)
        return 2
    except _Invalid as error:
        print(f"[x] invalid test-duplicate baseline: {error}", file=sys.stderr)
        return 2

    try:
        module_count, digests, qualnames = scan(root, SCOPE)
    except (OSError, UnicodeError, ValueError, SyntaxError) as error:
        print(f"[x] test-duplicate scan failed: {error}", file=sys.stderr)
        return 2
    bodies = duplicate_bodies(digests)
    names = duplicate_names(qualnames)
    result = compare(bodies, names, allowed_bodies, allowed_names, set(digests))
    if result.growth:
        for diagnostic in result.growth:
            print(f"[x] {diagnostic}", file=sys.stderr)
        if args.update_baseline:
            print(
                "[x] refusing to update test-duplicate baseline with new duplicates; "
                "fix the listed tests instead",
                file=sys.stderr,
            )
        return 1
    changes = len(result.stale) + len(result.moved)
    if changes and args.update_baseline:
        updated: dict[str, object] = {
            "algorithm": ALGORITHM,
            "algorithm_doc": ALGORITHM_DOC,
            "duplicate_bodies": baseline_layout(result.keep["duplicate_bodies"]),
            "duplicate_names": baseline_layout(result.keep["duplicate_names"]),
            "schema_version": SCHEMA_VERSION,
            "scope": SCOPE,
        }
        if provisional is not None:
            updated["provisional"] = provisional
        try:
            write_baseline(baseline, updated, label="test-duplicate")
        except BaselineError as error:
            print(f"[x] {error}", file=sys.stderr)
            return 2
        print(
            f"[+] updated test-duplicate baseline: removed {len(result.stale)} stale and "
            f"re-keyed {len(result.moved)} moved entr{'y' if changes == 1 else 'ies'}"
        )
    elif changes:
        for entry in result.stale:
            print(f"[i] stale test-duplicate baseline entry (no longer duplicated): {entry}")
        for new, old in sorted(result.moved.items()):
            print(f"[i] moved test-duplicate baseline entry: {old} -> {new}")
        print(
            f"[i] {changes} stale or moved entr{'y' if changes == 1 else 'ies'}; "
            f"optional cleanup: {UPDATE_COMMAND}"
        )
    elif args.update_baseline:
        print("[i] test-duplicate baseline already current; no update written")

    print(
        f"[+] test duplicate ratchet clean: {module_count} files, {len(digests)} tests, "
        f"{len(bodies)} duplicate body group(s), "
        f"{sum(len(ids) for ids in names.values())} duplicate Class::name test(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
