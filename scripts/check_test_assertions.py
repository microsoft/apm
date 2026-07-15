#!/usr/bin/env python3
"""Enforce bounded no-growth ceilings for two assertion-quality forms."""

from __future__ import annotations

import argparse
import ast
import sys
from collections import defaultdict
from pathlib import Path

from ratchet_baseline import (
    BaselineError,
    load_baseline,
    validate_provisional,
    write_baseline,
)
from test_file_inventory import tracked_python_paths

RULE_AQ001 = "AQ001_constant_assertion"
RULE_AQ002 = "AQ002_broad_pytest_raises"
RULES = (RULE_AQ001, RULE_AQ002)


def scan(root: Path) -> dict[str, dict[str, list[int]]]:
    """Locate only AQ001 constant assertions and AQ002 broad pytest.raises."""
    observed: dict[str, defaultdict[str, list[int]]] = {rule: defaultdict(list) for rule in RULES}
    for path in tracked_python_paths(
        root,
        scope=("tests/*.py", "tests/**/*.py"),
    ):
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assert)
                and isinstance(node.test, ast.Constant)
                and (node.test.value is True or node.test.value is False or node.test.value is None)
            ):
                observed[RULE_AQ001][relative].append(node.lineno)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "raises"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "pytest"
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in {"Exception", "BaseException"}
            ):
                observed[RULE_AQ002][relative].append(node.lineno)
    return {
        rule: {path: sorted(lines) for path, lines in sorted(observed[rule].items())}
        for rule in RULES
    }


def compare(
    observed: dict[str, dict[str, list[int]]],
    allowed: dict[str, dict[str, int]],
) -> tuple[list[str], list[str]]:
    """Return growth and stale-reduction diagnostics."""
    growth: list[str] = []
    stale: list[str] = []
    for rule in RULES:
        paths = set(observed[rule]) | set(allowed[rule])
        for path in sorted(paths):
            lines = observed[rule].get(path, [])
            current = len(lines)
            ceiling = allowed[rule].get(path, 0)
            if current > ceiling:
                if rule == RULE_AQ001:
                    problem = "constant assertion does not test behavior"
                    remedy = "replace it with an assertion over observed output or state"
                else:
                    problem = "pytest.raises catches Exception or BaseException"
                    remedy = "assert the narrow exception type raised by this scenario"
                locations = ", ".join(f"{path}:{line}" for line in lines)
                growth.append(
                    f"{locations}: {rule}: {problem} "
                    f"(observed {current}, allowed {ceiling}); {remedy}"
                )
            elif current < ceiling:
                stale.append(f"{path}: {rule} reduced to {current} from {ceiling}")
    return growth, stale


def main(argv: list[str] | None = None) -> int:
    """Check the baseline, or tighten it when only reductions are observed."""
    parser = argparse.ArgumentParser(
        description="Check AQ001/AQ002 assertion-quality no-growth ceilings."
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--update-baseline", action="store_true")
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
        else root / "tests" / "quality" / "assertion_quality_baseline.json"
    )
    try:
        payload = load_baseline(baseline, label="assertion-quality")
        provisional = validate_provisional(
            payload,
            baseline,
            allow=args.allow_provisional,
            label="assertion-quality",
        )
    except BaselineError as error:
        print(f"[x] {error}", file=sys.stderr)
        return 2
    required_keys = {"schema_version", "rules"}
    allowed_keys = required_keys | {"provisional"}
    if not required_keys <= set(payload) or not set(payload) <= allowed_keys:
        print(
            "[x] invalid assertion-quality baseline: unexpected object shape",
            file=sys.stderr,
        )
        return 2
    if payload["schema_version"] != 1:
        print(
            "[x] invalid assertion-quality baseline: schema_version must be 1",
            file=sys.stderr,
        )
        return 2

    raw_rules = payload["rules"]
    if not isinstance(raw_rules, dict) or set(raw_rules) != set(RULES):
        print(
            "[x] invalid assertion-quality baseline: both named rules are required",
            file=sys.stderr,
        )
        return 2
    allowed: dict[str, dict[str, int]] = {}
    for rule in RULES:
        by_path = raw_rules[rule]
        if not isinstance(by_path, dict):
            print(
                f"[x] invalid assertion-quality baseline: {rule} must be an object",
                file=sys.stderr,
            )
            return 2
        normalized: dict[str, int] = {}
        for path, count in by_path.items():
            if not isinstance(path, str) or not path or type(count) is not int or count < 0:
                print(
                    f"[x] invalid assertion-quality baseline: bad count in {rule}",
                    file=sys.stderr,
                )
                return 2
            normalized[path] = count
        allowed[rule] = normalized

    try:
        observed = scan(root)
    except (OSError, UnicodeError, SyntaxError, ValueError) as error:
        print(f"[x] assertion-quality scan failed: {error}", file=sys.stderr)
        return 2
    growth, stale = compare(observed, allowed)
    if growth:
        for diagnostic in growth:
            print(f"[x] {diagnostic}", file=sys.stderr)
        if args.update_baseline:
            print(
                "[x] refusing to update assertion baseline with new debt; "
                "fix the listed assertions instead",
                file=sys.stderr,
            )
        return 1
    if stale and not args.update_baseline:
        for diagnostic in stale:
            print(f"[x] {diagnostic}", file=sys.stderr)
        command = "uv run --frozen python scripts/check_test_assertions.py --update-baseline"
        print(
            "[x] assertion baseline is stale after resolved findings. "
            f"Review the reductions, then run '{command}'.",
            file=sys.stderr,
        )
        return 1
    if stale:
        reductions = sum(
            allowed[rule].get(path, 0) - len(observed[rule].get(path, []))
            for rule in RULES
            for path in allowed[rule]
        )
        updated_rules = {
            rule: {path: len(lines) for path, lines in observed[rule].items()} for rule in RULES
        }
        updated: dict[str, object] = {
            "schema_version": 1,
            "rules": updated_rules,
        }
        if provisional is not None:
            updated["provisional"] = provisional
        try:
            write_baseline(
                baseline,
                updated,
                label="assertion",
            )
        except BaselineError as error:
            print(f"[x] {error}", file=sys.stderr)
            return 2
        print(
            f"[+] updated assertion-quality baseline: removed {reductions} resolved occurrence(s)"
        )
    elif args.update_baseline:
        print("[i] assertion-quality baseline already current; no update written")

    aq001_total = sum(len(lines) for lines in observed[RULE_AQ001].values())
    aq002_total = sum(len(lines) for lines in observed[RULE_AQ002].values())
    print(f"[+] assertion-quality ratchet clean: AQ001={aq001_total}, AQ002={aq002_total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
