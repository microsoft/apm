#!/usr/bin/env python3
"""Enforce Stage 2 Ruff complexity limits with a shrink-only baseline.

New and changed functions must stay within the Stage 2 limits below. Functions
that already exceeded them when the ratchet landed (microsoft/apm#1076) are
grandfathered in ``tests/quality/complexity_baseline.json``, keyed by file path
and function qualname (never line numbers). The check fails when:

- a violation appears that the baseline does not list (new debt);
- the baseline lists an entry the base ref did not (baseline growth);
- a listed violation is gone but still in the baseline (stale entry).

Stale entries are pruned with ``--update-baseline``, which never adds entries.
``noqa`` comments do not exempt a function from the ratchet.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from pathlib import Path

from ratchet_baseline import (
    BaselineError,
    load_baseline,
    load_baseline_at_ref,
    validate_provisional,
    write_baseline,
)

LABEL = "complexity"
LIMITS = {
    "max-args": 12,
    "max-branches": 40,
    "max-complexity": 35,
    "max-returns": 8,
    "max-statements": 120,
}
RULES = ["C901", "PLR0911", "PLR0912", "PLR0913", "PLR0915"]
# Mirrors the path list of the "Ruff lint" step in .github/workflows/ci.yml.
SCOPE = [
    "scripts/architecture_linter",
    "scripts/lint_architecture_boundaries.py",
    "scripts/windows_native_symlink_probe_entry.py",
    "src",
    "tests",
]
_RUFF_SETTINGS = {
    "max-args": "lint.pylint.max-args",
    "max-branches": "lint.pylint.max-branches",
    "max-complexity": "lint.mccabe.max-complexity",
    "max-returns": "lint.pylint.max-returns",
    "max-statements": "lint.pylint.max-statements",
}
UPDATE_COMMAND = "uv run --frozen --extra dev python scripts/check_complexity_ratchet.py"

Entry = tuple[str, str, str]


def _function_qualnames(source: str, filename: str) -> dict[int, list[str]]:
    """Map each function definition line to Python-style qualnames."""
    tree = ast.parse(source, filename=filename)
    by_line: dict[int, list[str]] = {}

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualname = f"{prefix}{child.name}"
                by_line.setdefault(child.lineno, []).append(qualname)
                visit(child, f"{qualname}.<locals>.")
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")
            else:
                visit(child, prefix)

    visit(tree, "")
    return by_line


def observe(root: Path) -> set[Entry]:
    """Run Ruff with the Stage 2 limits and key each violation by function."""
    command = [
        sys.executable,
        "-m",
        "ruff",
        "check",
        "--no-cache",
        "--ignore-noqa",
        "--exit-zero",
        "--output-format",
        "json",
        "--select",
        ",".join(RULES),
    ]
    for name, value in sorted(LIMITS.items()):
        command += ["--config", f"{_RUFF_SETTINGS[name]} = {value}"]
    command += SCOPE
    result = subprocess.run(
        command, cwd=root, capture_output=True, text=True, encoding="utf-8", check=False
    )
    if result.returncode != 0:
        raise BaselineError(f"ruff failed ({result.returncode}): {result.stderr.strip()}")
    diagnostics = json.loads(result.stdout)

    resolved_root = root.resolve()
    qualnames: dict[Path, dict[int, list[str]]] = {}
    observed: set[Entry] = set()
    for diagnostic in diagnostics:
        code = diagnostic.get("code")
        path = Path(diagnostic["filename"]).resolve()
        relative = path.relative_to(resolved_root).as_posix()
        row = diagnostic["location"]["row"]
        if code not in RULES:
            raise BaselineError(f"unexpected ruff diagnostic {code} at {relative}:{row}")
        if path not in qualnames:
            qualnames[path] = _function_qualnames(path.read_text(encoding="utf-8"), relative)
        names = qualnames[path].get(row)
        if not names:
            raise BaselineError(f"no function definition for {code} at {relative}:{row}")
        observed.update((relative, name, code) for name in names)
    return observed


def _validated_entries(payload: dict[str, object], where: str) -> set[Entry]:
    """Validate the baseline shape and return its (path, qualname, rule) entries."""
    required = {"limits", "rules", "schema_version", "scope", "violations"}
    if not required <= set(payload) or not set(payload) <= required | {"provisional"}:
        raise BaselineError(f"invalid {LABEL} baseline{where}: unexpected object shape")
    if payload["schema_version"] != 1:
        raise BaselineError(f"invalid {LABEL} baseline{where}: unsupported schema")
    if payload["limits"] != LIMITS or payload["rules"] != RULES:
        raise BaselineError(f"invalid {LABEL} baseline{where}: limits must remain exact")
    if payload["scope"] != SCOPE:
        raise BaselineError(f"invalid {LABEL} baseline{where}: scope must remain exact")
    violations = payload["violations"]
    if not isinstance(violations, dict):
        raise BaselineError(f"invalid {LABEL} baseline{where}: violations must be an object")
    entries: set[Entry] = set()
    for path, functions in violations.items():
        if not isinstance(functions, dict) or not functions:
            raise BaselineError(f"invalid {LABEL} baseline{where}: malformed entry {path}")
        for qualname, rules in functions.items():
            if (
                not isinstance(rules, list)
                or not rules
                or rules != sorted(set(rules))
                or not set(rules) <= set(RULES)
            ):
                raise BaselineError(
                    f"invalid {LABEL} baseline{where}: rules for {path}::{qualname} "
                    "must be sorted, unique, known rule codes"
                )
            entries.update((path, qualname, rule) for rule in rules)
    return entries


def _serialize(entries: set[Entry]) -> dict[str, dict[str, list[str]]]:
    violations: dict[str, dict[str, list[str]]] = {}
    for path, qualname, rule in sorted(entries):
        violations.setdefault(path, {}).setdefault(qualname, []).append(rule)
    return violations


def _format(entries: set[Entry]) -> str:
    return "\n".join(f"  - {path}::{qualname} {rule}" for path, qualname, rule in sorted(entries))


def _report(message: str, entries: set[Entry] | None = None) -> None:
    print(f"[x] {message}", file=sys.stderr)
    if entries:
        print(_format(entries), file=sys.stderr)


def _check_base_growth(root: Path, baseline: Path, ref: str, current: set[Entry]) -> bool:
    """Return True when the working baseline adds entries absent at ``ref``."""
    base_payload = load_baseline_at_ref(root, baseline, ref, label=LABEL)
    if base_payload is None:
        print(f"[i] no {LABEL} baseline at {ref}; skipping baseline growth check")
        return False
    added = current - _validated_entries(base_payload, f" at {ref}")
    if added:
        _report(
            f"{LABEL} baseline grew against {ref}. Bring these functions within the "
            "Stage 2 limits instead of grandfathering them:",
            added,
        )
    return bool(added)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check Stage 2 Ruff complexity limits against a shrink-only baseline."
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument(
        "--base-ref",
        help="Git ref whose committed baseline the working baseline may not exceed.",
    )
    parser.add_argument(
        "--allow-provisional",
        action="store_true",
        help="Allow explicitly provisional metadata during non-final checks.",
    )
    return parser.parse_args(argv)


def _run(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    baseline = (
        args.baseline.resolve()
        if args.baseline is not None
        else root / "tests" / "quality" / "complexity_baseline.json"
    )
    payload = load_baseline(baseline, label=LABEL)
    provisional = validate_provisional(
        payload, baseline, allow=args.allow_provisional, label=LABEL
    )
    allowed = _validated_entries(payload, "")
    missing = [entry for entry in SCOPE if not (root / entry).exists()]
    if missing:
        raise BaselineError(f"{LABEL} scope path(s) missing: {', '.join(missing)}")

    grew = args.base_ref is not None and _check_base_growth(
        root, baseline, args.base_ref, allowed
    )
    observed = observe(root)
    new = observed - allowed
    stale = allowed - observed
    if new:
        for path, qualname, rule in sorted(new):
            _report(f"new complexity violation: {path}::{qualname} {rule}")
        _report(
            f"Stage 2 limits are {json.dumps(LIMITS, sort_keys=True)}. Simplify the "
            "listed function(s); do not expand the baseline."
        )
        if args.update_baseline:
            _report(f"refusing to update {LABEL} baseline with new violations")
        return 1
    if grew:
        return 1
    if stale and not args.update_baseline:
        _report(f"{LABEL} baseline is stale; these violations are fixed:", stale)
        _report(f"Review the reductions, then run '{UPDATE_COMMAND} --update-baseline'.")
        return 1
    if stale:
        updated: dict[str, object] = {
            "limits": LIMITS,
            "rules": RULES,
            "schema_version": 1,
            "scope": SCOPE,
            "violations": _serialize(observed),
        }
        if provisional is not None:
            updated["provisional"] = provisional
        write_baseline(baseline, updated, label=LABEL)
        print(f"[+] updated {LABEL} baseline: removed {len(stale)} resolved violation(s)")
    elif args.update_baseline:
        print(f"[i] {LABEL} baseline already current; no update written")
    print(f"[+] complexity ratchet clean: {len(observed)} grandfathered violation(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Check the ratchet, or prune resolved entries with --update-baseline."""
    args = _parse_args(argv)
    try:
        return _run(args)
    except (BaselineError, OSError, SyntaxError, ValueError) as error:
        print(f"[x] {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
