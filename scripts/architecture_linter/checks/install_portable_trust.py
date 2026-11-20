"""Portable executable-trust guards (#3902).

Three owners keep "may this dependency run code here?" single-sourced:

* item admission  -- ``security/trust.py`` alone reads pinned item grants;
* fingerprint     -- ``security/trust_fingerprint.py`` alone hashes;
* MCP admission   -- every ``CurrentMcpConfigView.derive`` call passes the
  owner's ``admission=``; no command passes a literal trust boolean;
* hook pin parity -- ``install --frozen`` and ``audit --ci`` verify locked
  hook approval pins through one owner route, and nothing else does (#4188).
"""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.install_deployment_shared import (
    _facts_for,
    _lines,
    _present,
    _python_paths,
    _rule,
    _summary,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Rule, Violation

GUARD_ITEM_ADMISSION = "install-deployment-executable-item-admission"
GUARD_TRUST_FINGERPRINT = "install-deployment-trust-fingerprint"
GUARD_MCP_ADMISSION = "install-deployment-mcp-admission"
GUARD_HOOK_PIN_PARITY = "install-deployment-hook-pin-frozen-audit-parity"

_SRC = "src/apm_cli/"
_TRUST = "src/apm_cli/security/trust.py"
_FINGERPRINT = "src/apm_cli/security/trust_fingerprint.py"
_EXECUTABLES = "src/apm_cli/security/executables.py"
_MCP_VIEW = "src/apm_cli/integration/mcp_config_view.py"

# Tokens that read pinned item grants; only the trust owner (and the parser
# that produces them) may touch them.
_ITEM_TOKENS = ("project_items", "user_items", "split_item_grants(", "item_pin(")
_ITEM_PRODUCERS = (_TRUST, _EXECUTABLES)

_ITEM_ROUTES: dict[str, tuple[str, ...]] = {
    _TRUST: (
        "def admit(",
        "def item_pin(",
        "def hook_file_filter(",
        "def record_pins(",
        "def package_type_verdicts(",
        "def admit_native_plugin(",
        "current = native_plugin_fingerprint(Path(plugin_root))",
    ),
    _EXECUTABLES: (
        "return entry.get(exec_type, False) is True",
        "if matches and entry.get(exec_type, False) is True:",
        "project_allow, project_items = split_item_grants(project_raw_allow)",
    ),
    "src/apm_cli/install/exec_gate.py": (
        "hooks_package_admissible(allow_executables, tuple(candidate_keys), package_info)",
        "return hook_file_filter(allow_executables, keys, Path(package_info.install_path))",
        "verdict = admit_native_plugin(",
        "if native_install(package_info):",
        "return _native_approval(",
    ),
    "src/apm_cli/install/deployment_plan.py": (
        "hook_file_admitted=hook_filter,",
        "_warn_parked_plugin(package_name, package_info, allow_executables, diagnostics, logger, ctx)",
    ),
    "src/apm_cli/integration/hook_reintegration.py": ("hook_file_admitted=hook_file_admission(",),
    "src/apm_cli/commands/approve.py": (
        "narrowed = record_pins(allow, identity",
        "return package_type_verdicts(",
    ),
    "src/apm_cli/install/native_plugin_admission.py": ("native=True,",),
}
# Type-level resolvers cannot see pins; outside security/ every per-type
# verdict comes from trust.package_type_verdicts.
_PIN_BLIND = ("resolve_exec_decision(", "exec_status_for_declaration(")
_SECURITY = "src/apm_cli/security/"
# A live-loaded plugin is admitted as one whole-root item, never by a bool lookup.
_NATIVE_ADMISSION = "src/apm_cli/install/native_plugin_admission.py"
_BOOL_LOOKUPS = ("is_package_approved(", "_map_grants(", "allow_executables.get(")

# Files that take fingerprints from the owner and must never hash themselves.
_NO_HASHLIB = (
    _TRUST,
    _EXECUTABLES,
    _MCP_VIEW,
    "src/apm_cli/install/exec_gate.py",
    "src/apm_cli/commands/approve.py",
    "src/apm_cli/integration/hook_integrator.py",
)
_FINGERPRINT_ROUTES: dict[str, tuple[str, ...]] = {
    _FINGERPRINT: (
        "def canonical_digest(",
        "def mcp_definition_fingerprint(",
        "def hook_file_fingerprint(",
        "def evidence_fingerprint(",
        "def local_bundle_digest(",
        "def native_plugin_fingerprint(",
        # Approval pins leave the exec bit out so they hold across operating
        # systems; the mode-aware hash is the per-machine integrity check.
        "return compute_native_plugin_pin(plugin_root)",
    ),
    _MCP_VIEW: ("return evidence_fingerprint(payload)",),
    _EXECUTABLES: ("digest = local_bundle_digest(source_dir, lockfile)",),
}
_HASHLIB_IMPORT = re.compile(r"^\s*(?:import hashlib\b|from hashlib import)")

_DERIVE_CALL = "CurrentMcpConfigView.derive("
_DERIVE_WINDOW = 16
_MCP_ROUTES: dict[str, tuple[str, ...]] = {
    _TRUST: ("class McpAdmission:", "def for_project(", "def admit_all("),
    _MCP_VIEW: ("admission: McpAdmission,", "verdict = admission.admit(subject)"),
    "src/apm_cli/install/service.py": ("admission=admission,",),
}


_EXEC_GATE = "src/apm_cli/install/exec_gate.py"
_SERVICE = "src/apm_cli/install/service.py"
_CI_CHECKS = "src/apm_cli/policy/ci_checks.py"
_HOOK_PIN_ROUTES: dict[str, tuple[str, ...]] = {
    _TRUST: ("def recorded_hook_pin_mismatches(",),
    _EXEC_GATE: ("def locked_hook_pin_mismatches(", "recorded_hook_pin_mismatches("),
    _SERVICE: ("mismatches = locked_hook_pin_mismatches(lockfile, modules_dir, trust_ctx)",),
    "src/apm_cli/install/phases/integrate.py": ("InstallService.enforce_frozen_hook_pins(",),
    _CI_CHECKS: ("mismatches = locked_hook_pin_mismatches(", "_check_hook_approval_pins("),
}
# Closed world: the owner call and its lock walk have exactly these callers.
_HOOK_PIN_CALLERS: dict[str, frozenset[str]] = {
    "recorded_hook_pin_mismatches(": frozenset({_TRUST, _EXEC_GATE}),
    "locked_hook_pin_mismatches(": frozenset({_EXEC_GATE, _SERVICE, _CI_CHECKS}),
}


def _missing(provider: FactsProvider, rule_id: str, routes: dict[str, tuple[str, ...]]):
    findings: list[Violation] = []
    for path, tokens in routes.items():
        facts, failures = _facts_for(provider, path, rule_id)
        if failures:
            findings.extend(failures)
            continue
        missing = [token for token in tokens if not _present(facts, token)]
        if missing:
            findings.append(
                _summary(rule_id, path, "Canonical trust routing is missing: " + ", ".join(missing))
            )
    return findings


def check_item_admission(provider: FactsProvider) -> tuple[Violation, ...]:
    """Pinned item grants are read only by security/trust.py."""
    findings = _missing(provider, GUARD_ITEM_ADMISSION, _ITEM_ROUTES)
    facts, failures = _facts_for(provider, _NATIVE_ADMISSION, GUARD_ITEM_ADMISSION)
    if not failures:
        found = [token for token in _BOOL_LOOKUPS if _present(facts, token)]
        if found:
            findings.append(
                _summary(
                    GUARD_ITEM_ADMISSION,
                    _NATIVE_ADMISSION,
                    "Native plugin hooks are admitted per file by security/trust.py; remove "
                    + ", ".join(found),
                )
            )
    for path in _python_paths(provider, _SRC):
        if path in _ITEM_PRODUCERS:
            continue
        facts, failures = _facts_for(provider, path, GUARD_ITEM_ADMISSION)
        if failures:
            continue
        found = [token for token in _ITEM_TOKENS if _present(facts, token)]
        if not path.startswith(_SECURITY):
            found += [
                token
                for token in _PIN_BLIND
                if any(
                    token in line and not line.lstrip().startswith(("#", "def "))
                    for line in _lines(facts)
                )
            ]
        if found:
            findings.append(
                _summary(
                    GUARD_ITEM_ADMISSION,
                    path,
                    "Pinned item grants are decided by security/trust.py; remove "
                    + ", ".join(found),
                )
            )
    return tuple(findings)


def check_trust_fingerprint(provider: FactsProvider) -> tuple[Violation, ...]:
    """Every trust fingerprint is computed by security/trust_fingerprint.py."""
    findings = _missing(provider, GUARD_TRUST_FINGERPRINT, _FINGERPRINT_ROUTES)
    for path in _python_paths(provider, _SRC):
        if path in (_FINGERPRINT, "src/apm_cli/utils/content_hash.py"):
            continue
        facts, failures = _facts_for(provider, path, GUARD_TRUST_FINGERPRINT)
        if not failures and any("compute_native_plugin_pin(" in line for line in _lines(facts)):
            findings.append(
                _summary(
                    GUARD_TRUST_FINGERPRINT,
                    path,
                    "Native plugin approval pins come only from native_plugin_fingerprint",
                )
            )
    for path in _NO_HASHLIB:
        facts, failures = _facts_for(provider, path, GUARD_TRUST_FINGERPRINT)
        if failures:
            findings.extend(failures)
            continue
        if any(_HASHLIB_IMPORT.search(line) for line in _lines(facts)):
            findings.append(
                _summary(
                    GUARD_TRUST_FINGERPRINT,
                    path,
                    "Trust fingerprints come from security/trust_fingerprint.py; "
                    "do not import hashlib here",
                )
            )
    return tuple(findings)


def check_mcp_admission(provider: FactsProvider) -> tuple[Violation, ...]:
    """Every MCP view derivation passes the trust owner's admission."""
    findings = _missing(provider, GUARD_MCP_ADMISSION, _MCP_ROUTES)
    for path in _python_paths(provider, _SRC):
        facts, failures = _facts_for(provider, path, GUARD_MCP_ADMISSION)
        if failures:
            continue
        lines = _lines(facts)
        if any("trust_transitive_self_defined" in line for line in lines):
            findings.append(
                _summary(
                    GUARD_MCP_ADMISSION,
                    path,
                    "MCP trust is decided by McpAdmission; remove trust_transitive_self_defined",
                )
            )
        for index, line in enumerate(lines):
            if _DERIVE_CALL not in line or line.lstrip().startswith(("def ", "#")):
                continue
            window = lines[index : index + _DERIVE_WINDOW]
            if not any("admission=" in candidate for candidate in window):
                findings.append(
                    _summary(
                        GUARD_MCP_ADMISSION,
                        path,
                        "CurrentMcpConfigView.derive must receive admission= from "
                        "security/trust.py",
                    )
                )
    return tuple(findings)


def check_hook_pin_parity(provider: FactsProvider) -> tuple[Violation, ...]:
    """Frozen install and audit --ci verify hook pins through one owner route."""
    findings = _missing(provider, GUARD_HOOK_PIN_PARITY, _HOOK_PIN_ROUTES)
    for path in _python_paths(provider, _SRC):
        facts, failures = _facts_for(provider, path, GUARD_HOOK_PIN_PARITY)
        if failures:
            continue
        stray = [
            token
            for token, callers in _HOOK_PIN_CALLERS.items()
            if path not in callers and _present(facts, token)
        ]
        if stray:
            findings.append(
                _summary(
                    GUARD_HOOK_PIN_PARITY,
                    path,
                    "Locked hook pins are verified only for install --frozen and audit --ci "
                    "through install/exec_gate.py; remove " + ", ".join(stray),
                )
            )
    return tuple(findings)


RULES: tuple[Rule, ...] = (
    _rule(
        GUARD_ITEM_ADMISSION,
        "Pinned executable item grants are admitted only by security/trust.py.",
        check_item_admission,
    ),
    _rule(
        GUARD_TRUST_FINGERPRINT,
        "Trust fingerprints are computed only by security/trust_fingerprint.py.",
        check_trust_fingerprint,
    ),
    _rule(
        GUARD_MCP_ADMISSION,
        "Every MCP view derivation passes the trust owner's admission.",
        check_mcp_admission,
    ),
    _rule(
        GUARD_HOOK_PIN_PARITY,
        "install --frozen and audit --ci verify locked hook approval pins through one owner.",
        check_hook_pin_parity,
    ),
)

__all__ = ["RULES"]
