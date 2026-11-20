"""Guard: update and uninstall retire what the gates no longer allow, as install does.

``install/exec_gate.py::gate_retirements`` is the one owner of gate
monotonicity on the paths that skip install's own gate (#4444, #4445, #4446):
it judges org-policy dependency denies with ``policy_denied_locked_packages``
and recorded MCP servers with install's ``McpAdmission.for_project`` through
``CurrentMcpConfigView.consent_problems``. ``apm update`` asks it from its plan
callback, and the uninstall MCP cleanup derives its surviving servers with the
same admission and the same policy-deny owner, never an admit-all admission.
"""

from __future__ import annotations

from scripts.architecture_linter.checks.install_deployment_shared import _facts_for, _summary
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Violation

GUARD_GATE_RETIREMENT = "install-deployment-gate-retirement"
_OWNER = "src/apm_cli/install/exec_gate.py"
_UPDATE = "src/apm_cli/commands/update.py"
_UNINSTALL = "src/apm_cli/commands/uninstall/engine.py"

# (path, function, call qualname suffix that must appear inside it)
_REQUIRED = (
    (_OWNER, "gate_retirements", "policy_denied_locked_packages"),
    (_OWNER, "gate_retirements", "McpAdmission.for_project"),
    (_OWNER, "gate_retirements", "consent_problems"),
    (_UPDATE, "_gate_retirements_pending", "gate_retirements"),
    (_UPDATE, "_plan_callback", "_gate_retirements_pending"),
    (_UNINSTALL, "_cleanup_stale_mcp", "policy_denied_locked_packages"),
    (_UNINSTALL, "_cleanup_stale_mcp", "McpAdmission.for_project"),
)
_FORBIDDEN = ((_UNINSTALL, "_cleanup_stale_mcp", "McpAdmission.admit_all"),)


def _calls_in(facts: object, function: str) -> set[str] | None:
    ranges = [
        (item.line, item.end_line)
        for item in getattr(facts, "definitions", ())
        if item.name == function and item.kind in ("function", "async_function")
    ]
    if not ranges:
        return None
    return {
        call.qualname
        for call in getattr(facts, "calls", ())
        if any(low <= call.line <= high for low, high in ranges)
    }


def _has(calls: set[str], needle: str) -> bool:
    return any(call == needle or call.endswith(f".{needle}") for call in calls)


def check_gate_retirement(provider: FactsProvider) -> tuple[Violation, ...]:
    """Update and uninstall judge gate retirement through the one owner."""
    rule = GUARD_GATE_RETIREMENT
    findings: list[Violation] = []
    for path, function, needle in (*_REQUIRED, *_FORBIDDEN):
        facts, failures = _facts_for(provider, path, rule)
        if failures:
            findings.extend(failures)
            continue
        calls = _calls_in(facts, function)
        if calls is None:
            findings.append(_summary(rule, path, f"{function} not found"))
            continue
        required = (path, function, needle) in _REQUIRED
        if _has(calls, needle) is not required:
            verb = "must call" if required else "must not call"
            findings.append(_summary(rule, path, f"{function} {verb} {needle}"))
    return tuple(dict.fromkeys(findings))
