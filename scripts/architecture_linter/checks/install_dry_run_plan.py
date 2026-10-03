"""Prospective dry-run plan ownership analyzer."""

from __future__ import annotations

import ast
import re

from scripts.architecture_linter.checks.install_deployment_shared import (
    _SRC_PREFIX,
    _count_re,
    _duplicate_definition_lines,
    _facts_for,
    _present,
    _summary,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Violation

_GUARD_DRY_RUN_PLAN = "install-deployment-prospective-dry-run-plan"
_OWNER = "src/apm_cli/install/dry_run_plan.py"
_COMMAND = "src/apm_cli/commands/install.py"
_RENDERER = "src/apm_cli/install/presentation/dry_run.py"
_SELECTION_OWNER = "src/apm_cli/install/package_selection.py"
_RESOLVE = "src/apm_cli/install/phases/resolve.py"


def _selection_uses_interpreted_identity(provider: FactsProvider) -> bool:
    """Install and preview must preserve structured references through one owner."""
    for path, function_name in (
        (_OWNER, "ProspectiveInstallPlan.from_apm_package"),
        (_RESOLVE, "_apply_only_filter"),
    ):
        index = provider.tree_index(path)
        function = index.function(function_name) if index is not None else None
        if function is None:
            return False
        calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
        if not any(
            isinstance(call.func, ast.Name) and call.func.id == "selected_dependency_identity"
            for call in calls
        ):
            return False
        if any(
            isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "DependencyReference"
            and call.func.attr == "parse"
            for call in calls
        ):
            return False
    return True


def check_prospective_dry_run_plan(provider: FactsProvider) -> tuple[Violation, ...]:
    """Dry-run collection, selection, checks, rendering, and counts use one plan."""
    rule_id = _GUARD_DRY_RUN_PLAN
    owner, owner_fail = _facts_for(provider, _OWNER, rule_id)
    command, command_fail = _facts_for(provider, _COMMAND, rule_id)
    renderer, renderer_fail = _facts_for(provider, _RENDERER, rule_id)
    selection, selection_fail = _facts_for(provider, _SELECTION_OWNER, rule_id)
    failures = list(owner_fail) + list(command_fail) + list(renderer_fail) + list(selection_fail)
    if failures:
        return tuple(failures)

    class_pattern = re.compile(r"^class ProspectiveInstallPlan:")
    duplicates = _duplicate_definition_lines(
        provider,
        rule_id=rule_id,
        prefix=_SRC_PREFIX,
        pattern=class_pattern,
        owner=_OWNER,
        message="ProspectiveInstallPlan must remain the sole dry-run preview owner",
        respect_exempt=False,
    )
    identity_pattern = re.compile(r"^def selected_dependency_identity\(")
    duplicates += _duplicate_definition_lines(
        provider,
        rule_id=rule_id,
        prefix=_SRC_PREFIX,
        pattern=identity_pattern,
        owner=_SELECTION_OWNER,
        message="Install selector identity must remain owned by package_selection",
        respect_exempt=False,
    )
    contract_holds = (
        _count_re(owner, class_pattern) == 1
        and _count_re(selection, identity_pattern) == 1
        and _selection_uses_interpreted_identity(provider)
        and _present(owner, "def from_apm_package(")
        and _present(owner, "def with_allowed_lsp_dependencies(")
        and _present(owner, "selected_apm_dependencies=selected_apm_dependencies")
        and _present(owner, "lsp_dependencies=tuple(apm_package.get_lsp_dependencies())")
        and _present(command, "ProspectiveInstallPlan.from_apm_package(")
        and _present(command, "prospective_plan.with_allowed_lsp_dependencies(")
        and sum(
            "prospective_plan.selected_apm_dependencies" in line
            for line in getattr(command, "lines", ())
        )
        >= 3
        and _present(
            command,
            "mcp_deps=list(prospective_plan.selected_mcp_dependencies) or None",
        )
        and _present(command, "prospective_plan.dependency_counts")
        and _present(renderer, "plan: ProspectiveInstallPlan")
        and _present(renderer, "plan.selected_apm_dependencies if plan.should_install_apm else ()")
        and _present(renderer, "for dep in selected_apm_dependencies:")
        and _present(renderer, "for dep in plan.selected_mcp_dependencies:")
        and _present(renderer, "for dep in plan.selected_lsp_dependencies:")
        and not _present(renderer, "ProspectiveInstallPlan(")
        and not duplicates
    )
    if contract_holds:
        return ()
    return (
        _summary(
            rule_id,
            _OWNER,
            "Dry-run dependencies, selection, checks, rendering, and counts must route "
            "through ProspectiveInstallPlan and the shared package-selection identity owner",
        ),
        *duplicates,
    )


__all__ = ["_GUARD_DRY_RUN_PLAN", "check_prospective_dry_run_plan"]
