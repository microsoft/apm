"""LSP capability gate guard (#3727).

Declared LSP servers that no selected target can configure must fail before
any target write. ``install/lsp/integration.py`` owns that decision
(``require_lsp_capable_target``); install and update reach it from a preflight
that runs before the integrate phase and before any MCP write.
"""

from __future__ import annotations

from scripts.architecture_linter.checks.install_deployment_shared import (
    _facts_for,
    _present,
    _python_paths,
    _summary,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Violation

GUARD_LSP_CAPABILITY_GATE = "install-deployment-lsp-capability-gate"

_OWNER = "src/apm_cli/install/lsp/integration.py"
_INTEGRATE_PHASE = "src/apm_cli/install/phases/integrate.py"
_SERVICE_INTEGRATION = "src/apm_cli/install/service_integration.py"
_UPDATE_COMMAND = "src/apm_cli/commands/update.py"
_INSTALL_COMMAND = "src/apm_cli/commands/install.py"
_LOCAL_BUNDLE = "src/apm_cli/install/local_bundle_handler.py"

# Wording of the capability-gap refusal; only the owner may phrase it.
_GAP_MESSAGES = ("no effective target supports", "lsp.json cannot be configured")

_REQUIRED: dict[str, tuple[str, ...]] = {
    _OWNER: (
        "def require_lsp_capable_target(",
        "def preflight_lsp_targets(",
        "require_lsp_capable_target(target_runtimes)",
        "require_lsp_capable_target(supported_target_runtimes, bundle=True)",
        "require_lsp_capable_target(self.target_runtimes)",
    ),
    _INSTALL_COMMAND: ("require_lsp_targets=ctx.install_mode is InstallMode.ALL",),
    _UPDATE_COMMAND: ("require_lsp_targets=True",),
    _LOCAL_BUNDLE: ("require_lsp_capable_target(",),
}

# (gate token, first write token): the gate must come first in the file.
_ORDERED: dict[str, tuple[str, str]] = {
    _INTEGRATE_PHASE: ("preflight_lsp_targets(ctx)", "for dep_ref in deps_to_install:"),
    _SERVICE_INTEGRATION: ("prepared_lsp.require_capable_target()", "run_mcp_integration("),
    _UPDATE_COMMAND: ("prepared_lsp.require_capable_target()", "run_mcp_integration("),
}


def _first_line(facts: object, needle: str) -> int | None:
    lines = getattr(facts, "lines", ())
    return next((index for index, line in enumerate(lines) if needle in line), None)


def check_lsp_capability_gate(provider: FactsProvider) -> tuple[Violation, ...]:
    """The LSP capability gap is refused by one owner, before any target write."""
    rule_id = GUARD_LSP_CAPABILITY_GATE
    findings: list[Violation] = []
    for path in _python_paths(provider, "src/apm_cli/"):
        if path == _OWNER:
            continue
        facts, failures = _facts_for(provider, path, rule_id)
        if failures:
            findings.extend(failures)
            continue
        if any(_present(facts, message) for message in _GAP_MESSAGES):
            findings.append(
                _summary(
                    rule_id,
                    path,
                    "The LSP capability-gap refusal must come from "
                    "install/lsp/integration.py (require_lsp_capable_target)",
                )
            )
    for path, tokens in _REQUIRED.items():
        facts, failures = _facts_for(provider, path, rule_id)
        if failures:
            findings.extend(failures)
            continue
        missing = [token for token in tokens if not _present(facts, token)]
        if missing:
            findings.append(
                _summary(rule_id, path, "LSP capability gate is not wired: " + ", ".join(missing))
            )
    for path, (gate, write) in _ORDERED.items():
        facts, failures = _facts_for(provider, path, rule_id)
        if failures:
            findings.extend(failures)
            continue
        gate_line = _first_line(facts, gate)
        write_line = _first_line(facts, write)
        if gate_line is None or write_line is None or gate_line >= write_line:
            findings.append(
                _summary(
                    rule_id,
                    path,
                    f"The LSP capability gate ({gate}) must run before "
                    f"the first target write ({write})",
                )
            )
    return tuple(findings)
