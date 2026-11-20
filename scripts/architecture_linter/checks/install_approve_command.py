"""Guard the approve command APM prints for parked executables (#4154).

``security/trust.approve_command`` renders ``apm approve [-g] <package>`` (remediation
catalog ids ``executables-unapproved`` / ``executables-unapproved-user-scope``),
so a user-scope parked notice names a command that works there. Closed world:
no other module builds an ``apm approve <value>`` f-string unless allowlisted
below, and ``apm approve`` scans a local native plugin through
``approval_scan_dir`` -- the same unit the install gate parked.
"""

from __future__ import annotations

import ast

from scripts.architecture_linter.checks.contracts_test_shared import _summary
from scripts.architecture_linter.checks.install_deployment_shared import (
    _SRC_PREFIX,
    _python_paths,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts
from scripts.architecture_linter.models import Rule, Violation

RULE = "install-deployment-approve-command"
OWNER = "src/apm_cli/security/trust.py"
APPROVE = "src/apm_cli/commands/approve.py"
# (path, enclosing function) -> why it still spells the command; the
# remediation catalog (core/remediation.py, #4143) folds these in.
ALLOWLIST: dict[tuple[str, str], str] = {
    (APPROVE, "_confirm_first_approval"): "project first-approval prompt",
    (APPROVE, "_record_and_save"): "names the --user store flag of the grant it lifts",
    (APPROVE, "_lift_deny_hint"): "names the deny layer's own store flag",
    ("src/apm_cli/install/mcp/integration.py", "warn_one_off_trust"): "project one-off trust",
    ("src/apm_cli/security/executables.py", "_filter_declared_services"): "catalog in #4143",
    ("src/apm_cli/security/executables.py", "_item_error"): "grant parse error",
    ("src/apm_cli/security/executables.py", "_parse_grant_block"): "grant parse error",
    ("src/apm_cli/integration/mcp_config_view.py", "consent_problems"): "audit consent text",
    ("src/apm_cli/integration/canvas_integrator.py", "_emit_trust_block"): "catalog in #4143",
    ("src/apm_cli/policy/policy_checks.py", "_check_required_executable_untrusted"): "audit",
}


def _builds_approve_command(node: ast.JoinedStr) -> bool:
    """Whether an f-string spells ``apm approve `` followed by an interpolated value."""
    for current, following in zip(node.values, node.values[1:], strict=False):
        if (
            isinstance(current, ast.Constant)
            and isinstance(current.value, str)
            and current.value.endswith(("apm approve ", "apm approve -g "))
            and isinstance(following, ast.FormattedValue)
        ):
            return True
    return False


def _enclosing_functions(root: ast.AST) -> dict[int, str]:
    """Map each f-string to the name of its nearest enclosing function."""
    owners: dict[int, str] = {}

    def visit(node: ast.AST, function: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, child.name)
                continue
            if isinstance(child, ast.JoinedStr):
                owners[id(child)] = function
            visit(child, function)

    visit(root, "")
    return owners


def _open_coded(provider: FactsProvider) -> list[Violation]:
    findings: list[Violation] = []
    for path in _python_paths(provider, _SRC_PREFIX):
        if path == OWNER:
            continue
        index = provider.tree_index(path)
        if index is None or index.root is None:
            continue
        owners = _enclosing_functions(index.root)
        for node in ast.walk(index.root):
            if not isinstance(node, ast.JoinedStr) or not _builds_approve_command(node):
                continue
            function = owners.get(id(node), "")
            if (path, function) in ALLOWLIST:
                continue
            findings.append(
                _summary(
                    RULE,
                    path,
                    f"{function or '<module>'} spells an approve hint; use approve_command "
                    f"({OWNER})",
                )
            )
    return findings


def check_approve_command(provider: FactsProvider) -> tuple[Violation, ...]:
    """Parked-executable hints name the approve command for the install's scope."""
    findings = _open_coded(provider)
    facts, failures = checked_facts(provider, APPROVE, RULE, require_python=True)
    findings.extend(failures)
    if facts is not None and facts.tree_index is not None:
        scanner = facts.tree_index.function("scan_installed_executable_packages")
        if scanner is None or "approval_scan_dir(" not in ast.unparse(scanner):
            findings.append(
                _summary(RULE, APPROVE, "Scan local packages through approval_scan_dir")
            )
    return tuple(findings)


RULES = (
    Rule(
        id=RULE,
        group="contracts_tests",
        guard_ids=(RULE,),
        description="Parked-executable hints print the approve command for their scope.",
        check=check_approve_command,
    ),
)
