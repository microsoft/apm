"""Claude project LSP plugin architecture boundary."""

from __future__ import annotations

from scripts.architecture_linter.checks.install_deployment_shared import (
    _facts_for,
    _lines,
    _summary,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import source_text
from scripts.architecture_linter.models import Violation

_GUARD_CLAUDE_LSP_PLUGIN = "install-deployment-claude-lsp-plugin-ownership"

_LSP_INTEGRATOR = "src/apm_cli/integration/lsp_integrator.py"
_SKILL_SUPPORT = "src/apm_cli/integration/skill_support.py"
_EXECUTABLES = "src/apm_cli/security/executables.py"
_INSTALL_TEMPLATE = "src/apm_cli/install/template.py"
_INSTALL_COMMAND = "src/apm_cli/commands/install.py"
_UPDATE_COMMAND = "src/apm_cli/commands/update.py"

_PATHS = (
    _LSP_INTEGRATOR,
    _SKILL_SUPPORT,
    _EXECUTABLES,
    _INSTALL_TEMPLATE,
    _INSTALL_COMMAND,
    _UPDATE_COMMAND,
)

_REQUIRED_FRAGMENTS: tuple[tuple[str, str], ...] = (
    (_LSP_INTEGRATOR, "BaseIntegrator.resolve_deploy_path(relative_path, project_root)"),
    (_SKILL_SUPPORT, "LSPIntegrator.reserved_project_skill_names(skills_dir, project_root)"),
    (_EXECUTABLES, 'owner = getattr(dependency, "resolved_by", None)'),
    (_EXECUTABLES, 'approval_keys = getattr(dependency, "approval_keys", ())'),
    (_INSTALL_TEMPLATE, "trust_ctx = exec_trust_context_for_project("),
    (_INSTALL_COMMAND, "ctx.exec_allow_map = effective_exec_map_for_project("),
    (_UPDATE_COMMAND, "effective_allow_executables = effective_exec_map_for_project("),
)


def check_claude_lsp_plugin_ownership(provider: FactsProvider) -> tuple[Violation, ...]:
    """Require Claude project LSP writes, cleanup, and trust to share the LSP owner."""
    facts_by_path = {}
    failures: list[Violation] = []
    for path in _PATHS:
        facts, path_failures = _facts_for(provider, path, _GUARD_CLAUDE_LSP_PLUGIN)
        facts_by_path[path] = facts
        failures.extend(path_failures)
    if failures:
        return tuple(failures)

    defects: list[str] = []
    lsp_text = "\n".join(_lines(facts_by_path[_LSP_INTEGRATOR]))
    if lsp_text.count("def reserved_project_skill_names(") != 1:
        defects.append(f"{_LSP_INTEGRATOR} must define reserved_project_skill_names exactly once")
    for path, fragment in _REQUIRED_FRAGMENTS:
        if fragment not in source_text(facts_by_path[path]):
            defects.append(f"{path} is missing {fragment!r}")
    if not defects:
        return ()
    return (
        _summary(
            _GUARD_CLAUDE_LSP_PLUGIN,
            _LSP_INTEGRATOR,
            "Claude project LSP plugin writes, trust, and cleanup must route through "
            f"LSPIntegrator; {'; '.join(defects)}",
        ),
    )


__all__ = ["_GUARD_CLAUDE_LSP_PLUGIN", "check_claude_lsp_plugin_ownership"]
