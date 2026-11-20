#!/usr/bin/env python3
"""Enforce no-growth ceilings for assertion-quality and e2e-drive rules.

AQ001 constant assertions, AQ002 broad pytest.raises, AQ003 component/e2e
tests with no assertion on output, exit code or durable state, AQ004 tests
that patch a symbol defined in an owner module they import, AQ005 patches of
a process-global stdlib attribute through a module namespace, and AQ006 e2e
modules that do not drive the installed CLI (or use CliRunner, or patch an
owner module). Counts per file may only shrink; a reduction is reported as
stale (info) and never fails.
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections import defaultdict
from pathlib import Path

import test_file_inventory
from ratchet_baseline import (
    BaselineError,
    load_baseline,
    validate_provisional,
    write_baseline,
)
from test_body_hash import iter_test_functions
from test_file_inventory import tracked_python_paths
from test_static_facts import (
    asserts_observable_outcome,
    drives_installed_cli,
    module_markers,
    owner_patches,
    uses_cli_runner,
)

RULE_AQ001 = "AQ001_constant_assertion"
RULE_AQ002 = "AQ002_broad_pytest_raises"
RULE_AQ003 = "AQ003_unobserved_outcome"
RULE_AQ004 = "AQ004_patches_owner_it_imports"
RULE_AQ005 = "AQ005_global_patch_through_namespace"
RULE_AQ006 = "AQ006_e2e_not_driving_installed_cli"
RULES = (RULE_AQ001, RULE_AQ002, RULE_AQ003, RULE_AQ004, RULE_AQ005, RULE_AQ006)
_STDLIB_MODULES = frozenset(sys.stdlib_module_names)
RULE_TEXT = {
    RULE_AQ001: (
        "constant assertion does not test behavior",
        "replace it with an assertion over observed output or state",
    ),
    RULE_AQ002: (
        "pytest.raises catches Exception or BaseException",
        "assert the narrow exception type raised by this scenario",
    ),
    RULE_AQ003: (
        "component/e2e test asserts no user-visible output, exit code or durable state",
        "assert on stdout/stderr, the exit code, or files and state the command leaves "
        "behind, not only on mocks or return values",
    ),
    RULE_AQ004: (
        "test patches a symbol defined in an owner module it imports",
        "exercise the real owner and replace only its collaborators or I/O boundary",
    ),
    RULE_AQ005: (
        "patch target reaches a process-global stdlib attribute through another "
        "module's namespace, so the mock also sees every other thread",
        "patch an owner seam, or the stdlib target itself (e.g. 'time.sleep') when "
        "global scope is intended and no threads are involved",
    ),
    RULE_AQ006: (
        "e2e module does not drive the installed CLI only",
        "run the installed apm (ApmLifecycleRunner or the apm_binary fixture), drop "
        "CliRunner and in-process owner patches, or reclassify the module as component",
    ),
}


def _is_patch(node: ast.expr) -> bool:
    return (isinstance(node, ast.Name) and node.id == "patch") or (
        isinstance(node, ast.Attribute) and node.attr == "patch"
    )


def _patch_target(node: ast.Call) -> str | None:
    """The dotted target of patch()/patch.dict()/patch.multiple()/monkeypatch.setattr()."""
    func = node.func
    if isinstance(func, ast.Name):
        name, owner = func.id, None
    elif isinstance(func, ast.Attribute):
        name, owner = func.attr, func.value
    else:
        return None
    patches = (
        name == "patch" and (owner is None or isinstance(owner, (ast.Name, ast.Attribute)))
    ) or (name in {"dict", "multiple"} and owner is not None and _is_patch(owner))
    sets = name == "setattr" and isinstance(owner, ast.Name) and owner.id == "monkeypatch"
    if not (patches or sets):
        return None
    target = node.args[0] if node.args else None
    if target is None:
        target = next(
            (keyword.value for keyword in node.keywords if keyword.arg in {"target", "in_dict"}),
            None,
        )
    if isinstance(target, ast.Constant) and isinstance(target.value, str):
        return target.value
    return None


def _project_module(root: Path, dotted: str) -> bool:
    relative = Path(*dotted.split("."))
    return any(
        (base / relative).with_suffix(".py").is_file()
        or (base / relative / "__init__.py").is_file()
        for base in (root / "src", root)
    )


def global_patch_through_namespace(root: Path, target: str) -> bool:
    """True for '<project module>.<stdlib module>.<attr>' (e.g. 'pkg.mod.time.sleep').

    Such a target patches the process-global stdlib attribute, not the
    project module's, so the mock records calls from every thread.
    """
    parts = target.split(".")
    for index in range(1, len(parts) - 1):
        if (
            parts[index] in _STDLIB_MODULES
            and _project_module(root, ".".join(parts[:index]))
            and not _project_module(root, ".".join(parts[: index + 1]))
        ):
            return True
    return False


def _scan_module(
    root: Path,
    relative: str,
    tree: ast.Module,
    observed: dict[str, defaultdict[str, list[int]]],
    owner_cache: dict[Path, frozenset[str]],
) -> None:
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
        if isinstance(node, ast.Call):
            target = _patch_target(node)
            if target is not None and global_patch_through_namespace(root, target):
                observed[RULE_AQ005][relative].append(node.lineno)
    if not test_file_inventory.is_test_module_path(relative):
        return
    markers = module_markers(tree)
    if markers & {"component", "e2e"}:
        for function in iter_test_functions(tree, relative):
            if not asserts_observable_outcome(function.node):
                observed[RULE_AQ003][relative].append(function.node.lineno)
    for line, _target in owner_patches(root, tree, owner_cache, defined_only=True):
        observed[RULE_AQ004][relative].append(line)
    if "e2e" in markers:
        if not drives_installed_cli(tree):
            observed[RULE_AQ006][relative].append(1)
        if uses_cli_runner(tree):
            observed[RULE_AQ006][relative].append(1)
        for line, _target in owner_patches(root, tree, owner_cache, defined_only=False):
            observed[RULE_AQ006][relative].append(line)


def scan(root: Path) -> dict[str, dict[str, list[int]]]:
    """Locate every rule occurrence in tracked test files."""
    observed: dict[str, defaultdict[str, list[int]]] = {rule: defaultdict(list) for rule in RULES}
    owner_cache: dict[Path, frozenset[str]] = {}
    for path in tracked_python_paths(
        root,
        scope=("tests/*.py", "tests/**/*.py"),
    ):
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        _scan_module(root, relative, tree, observed, owner_cache)
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
                problem, remedy = RULE_TEXT[rule]
                locations = ", ".join(f"{path}:{line}" for line in lines)
                growth.append(
                    f"{locations}: {rule}: {problem} "
                    f"(observed {current}, allowed {ceiling}). Fix: {remedy}"
                )
            elif current < ceiling:
                stale.append(f"{path}: {rule} reduced to {current} from {ceiling}")
    return growth, stale


def main(argv: list[str] | None = None) -> int:
    """Check the baseline, or tighten it when only reductions are observed."""
    parser = argparse.ArgumentParser(
        description="Check AQ001-AQ006 assertion-quality no-growth ceilings."
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
            "[x] invalid assertion-quality baseline: every named rule is required",
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
            print(f"[i] stale assertion-quality baseline entry: {diagnostic}")
        command = "uv run --frozen python scripts/check_test_assertions.py --update-baseline"
        print(f"[i] optional cleanup of stale entries: {command}")
    elif stale:
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

    totals = ", ".join(
        f"{rule.split('_', 1)[0]}={sum(len(lines) for lines in observed[rule].values())}"
        for rule in RULES
    )
    print(f"[+] assertion-quality ratchet clean: {totals}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
