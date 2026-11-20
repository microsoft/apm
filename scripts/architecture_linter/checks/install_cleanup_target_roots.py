"""Closed-world guard: deploy cleanup never removes a target root (#4132).

``BaseIntegrator.cleanup_empty_parents`` is the one place install, update,
uninstall and prune prune empty parents of retired files, and it stops at
every target root (``_target_root_dirs``). Integration and install-phase
modules remove directories only through it, or at the closed set of
directory removals below, each scoped to a directory APM owns outright.
"""

from __future__ import annotations

from scripts.architecture_linter.checks.install_deployment_shared import (
    _facts_for,
    _python_paths,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Violation

GUARD_CLEANUP_TARGET_ROOTS = "install-deployment-cleanup-target-roots"
_OWNER = "src/apm_cli/integration/base_integrator.py"
_SCOPES = ("src/apm_cli/integration/", "src/apm_cli/install/phases/")
# (path, enclosing function) of every other directory removal in scope.
_DIRECTORY_REMOVALS = frozenset(
    {
        (_OWNER, "cleanup_empty_parents"),
        # A retired skill's own directory, never its skills root.
        ("src/apm_cli/integration/cleanup.py", "_safe_remove_skill_directory"),
        # LSP config directories the server spec created for its config file.
        ("src/apm_cli/integration/lsp_integrator.py", "_clean_target_config"),
    }
)


def check_cleanup_target_roots(provider: FactsProvider) -> tuple[Violation, ...]:
    """Keep empty-parent pruning behind the owner and its target-root stop."""
    rule_id = GUARD_CLEANUP_TARGET_ROOTS
    findings: list[Violation] = []
    facts, failures = _facts_for(provider, _OWNER, rule_id)
    findings.extend(failures)
    if not failures:
        owner = [item for item in facts.definitions if item.name == "cleanup_empty_parents"]
        if len(owner) != 1 or not any(
            call.qualname == "_target_root_dirs" and owner[0].line <= call.line <= owner[0].end_line
            for call in facts.calls
        ):
            findings.append(
                violation(rule_id, _OWNER, "cleanup_empty_parents must stop at _target_root_dirs")
            )
    for scope in _SCOPES:
        for path in _python_paths(provider, scope):
            facts, failures = _facts_for(provider, path, rule_id)
            findings.extend(failures)
            if failures:
                continue
            findings.extend(
                violation(
                    rule_id,
                    path,
                    f"{call.scope or '<module>'} removes a directory outside "
                    "BaseIntegrator.cleanup_empty_parents",
                    line=call.line,
                    column=call.column,
                )
                for call in facts.calls
                if call.qualname.rsplit(".", 1)[-1] in {"rmdir", "removedirs"}
                and (path, call.scope) not in _DIRECTORY_REMOVALS
            )
    return tuple(findings)
