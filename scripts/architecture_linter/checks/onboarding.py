"""Bound the onboarding writer to manifest metadata, never installation."""

from __future__ import annotations

import ast
from collections.abc import Iterable

from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, inventory_paths, violation
from scripts.architecture_linter.models import Rule, Violation

RULE_ID = "onboarding-metadata-only"
WRITER = "src/apm_cli/adopt/manifest_edit.py"
INIT = "src/apm_cli/commands/init.py"
_FORBIDDEN_MODULES = (
    "shutil",
    "subprocess",
    "requests",
    "httpx",
    "urllib",
    "converters",
    "provenance",
)
_MUTATIONS = frozenset(
    {
        "write_text",
        "write_bytes",
        "unlink",
        "rename",
        "replace",
        "rmdir",
        "copy",
        "copytree",
        "rmtree",
        "dump_yaml",
        "dump_yaml_roundtrip",
        "write_yaml_text_atomic",
        "mkdir",
        "run_install_pipeline",
        "run_install",
        "system",
        "exec",
        "eval",
        "open",
        "write",
    }
)


def check_onboarding(provider: FactsProvider) -> Iterable[Violation]:
    """Inspect only onboarding's bounded source set and its CLI facade."""
    paths = inventory_paths(
        provider, prefixes=("src/apm_cli/adopt/",), exact=("src/apm_cli/commands/discover.py", INIT)
    )
    for path in paths:
        if not path.endswith(".py"):
            continue
        facts, failures = checked_facts(provider, path, RULE_ID, require_python=True)
        yield from failures
        imports, calls = facts.imports, facts.calls
        if path == INIT:
            function = facts.tree_index.function("init")
            branches = (
                [
                    node
                    for node in function.body
                    if isinstance(node, ast.If)
                    and isinstance(node.test, ast.Name)
                    and node.test.id == "discover_flag"
                ]
                if isinstance(function, ast.FunctionDef)
                else []
            )
            if len(branches) != 1:
                yield violation(RULE_ID, path, "Init must retain its separate discovery branch.")
                continue
            branch = branches[0]
            lines = range(branch.lineno, (branch.end_lineno or branch.lineno) + 1)
            imports = tuple(item for item in imports if item.line in lines)
            calls = tuple(item for item in calls if item.line in lines)
            if not any(call.qualname == "run_discover" for call in calls):
                yield violation(RULE_ID, path, "Init discovery must delegate to run_discover.")
        for imported in imports:
            modules = (imported.module or "", *imported.names)
            if any(
                part in _FORBIDDEN_MODULES for module in modules for part in module.split(".")
            ) or any(
                "apm_cli.install" in module and module != "apm_cli.install.locking"
                for module in modules
            ):
                yield violation(
                    RULE_ID,
                    path,
                    "Onboarding must not import conversion, transport or execution.",
                    line=imported.line,
                )
        for call in calls:
            name = call.qualname.rsplit(".", 1)[-1]
            if name in _MUTATIONS:
                if path == WRITER and name in {"mkdir", "write_yaml_text_atomic"}:
                    continue
                if path == "src/apm_cli/adopt/render.py" and call.qualname == "sys.stdout.write":
                    continue
                yield violation(
                    RULE_ID,
                    path,
                    "Onboarding writes belong only to the consumer manifest writer.",
                    line=call.line,
                )
        if path == "src/apm_cli/adopt/materialize.py":
            if not any(call.qualname == "lifecycle_operation" for call in facts.calls):
                yield violation(
                    RULE_ID, path, "Consented manifest writes must hold the lifecycle lock."
                )
        if path == "src/apm_cli/adopt/discovery.py":
            admission_calls = [
                node
                for node in facts.tree_index.nodes
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "validate_apm_package"
            ]
            if not admission_calls or any(
                not any(
                    keyword.arg == "read_only"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                    for keyword in node.keywords
                )
                for node in admission_calls
            ):
                yield violation(
                    RULE_ID, path, "Discovery must use canonical read-only package admission."
                )


RULES = (
    Rule(
        id=RULE_ID,
        group="mutation_writes",
        guard_ids=(RULE_ID,),
        description="Onboarding is read-only except for the canonical consumer manifest writer.",
        check=check_onboarding,
    ),
)
