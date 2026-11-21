"""Closed-world guard: one owner decides whether a lockfile is required (#4050).

``install/plan.py::manifest_requires_lockfile`` answers whether a missing
lockfile is a converged state. ``apm install --frozen`` (structural check and
service integration) and the ``apm audit --ci`` lockfile-exists check consult
it, so they agree; no other function re-derives the answer from the
manifest's dependency accessors.
"""

from __future__ import annotations

from collections import defaultdict

from scripts.architecture_linter.checks.install_deployment_shared import (
    _facts_for,
    _python_paths,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Violation

GUARD_LOCKFILE_REQUIRED = "install-deployment-lockfile-required"
_OWNER = ("src/apm_cli/install/plan.py", "manifest_requires_lockfile")
_ROUTES = (
    ("src/apm_cli/install/service.py", "enforce_frozen"),
    ("src/apm_cli/install/service_integration.py", "run_service_integrations"),
    ("src/apm_cli/policy/ci_checks.py", "_check_lockfile_exists"),
)
_PACKAGE_PROBE = "has_any_apm_dependencies"
_SERVICE_PROBES = frozenset({"get_all_mcp_dependencies", "get_lsp_dependencies"})
_ENTRY = "manifest_requires_lockfile"
# Functions that probe both accessors for a different decision.
_OTHER_DECISIONS = frozenset(
    {
        # apm update: whether a manifest is service-only (routing, not presence).
        ("src/apm_cli/commands/update.py", "_handle_service_only_update"),
    }
)


def _calls_in(facts, name: str) -> set[str] | None:
    found = [item for item in facts.definitions if item.name == name]
    if len(found) != 1:
        return None
    return {
        call.qualname.rsplit(".", 1)[-1]
        for call in facts.calls
        if found[0].line <= call.line <= found[0].end_line
    }


def check_lockfile_required(provider: FactsProvider) -> tuple[Violation, ...]:
    """Route every lockfile-presence verdict through manifest_requires_lockfile."""
    rule_id = GUARD_LOCKFILE_REQUIRED
    findings: list[Violation] = []
    for path, name in (_OWNER, *_ROUTES):
        facts, failures = _facts_for(provider, path, rule_id)
        findings.extend(failures)
        if failures:
            continue
        calls = _calls_in(facts, name)
        needed = _PACKAGE_PROBE if (path, name) == _OWNER else _ENTRY
        if calls is None or needed not in calls:
            findings.append(violation(rule_id, path, f"{name} must call {needed}"))
    for path in _python_paths(provider, "src/apm_cli/"):
        facts, failures = _facts_for(provider, path, rule_id)
        findings.extend(failures)
        if failures:
            continue
        probes: dict[str, set[str]] = defaultdict(set)
        for call in facts.calls:
            probes[call.scope].add(call.qualname.rsplit(".", 1)[-1])
        findings.extend(
            violation(
                rule_id,
                path,
                f"{scope} re-derives whether a lockfile is required; call {_ENTRY}",
            )
            for scope, names in sorted(probes.items())
            if _PACKAGE_PROBE in names
            and names & _SERVICE_PROBES
            and (path, scope) != _OWNER
            and (path, scope) not in _OTHER_DECISIONS
        )
    return tuple(findings)
