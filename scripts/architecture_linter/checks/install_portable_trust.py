"""Portable executable-trust guards (#3902).

Three owners keep "may this dependency run code here?" single-sourced:

* item admission  -- ``security/trust.py`` alone reads pinned item grants;
* fingerprint     -- ``security/trust_fingerprint.py`` alone hashes;
* MCP admission   -- every ``CurrentMcpConfigView.derive`` call passes the
  owner's ``admission=``; no command passes a literal trust boolean.
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
    _TRUST: ("def admit(", "def item_pin(", "def hook_file_filter(", "def record_pins("),
    _EXECUTABLES: (
        "return entry.get(exec_type, False) is True",
        "if matches and entry.get(exec_type, False) is True:",
        "project_allow, project_items = split_item_grants(project_raw_allow)",
    ),
    "src/apm_cli/install/exec_gate.py": (
        "hooks_package_admissible(allow_executables, tuple(candidate_keys))",
        "return hook_file_filter(allow_executables, keys, Path(package_info.install_path))",
    ),
    "src/apm_cli/install/deployment_plan.py": ("hook_file_admitted=hook_filter,",),
    "src/apm_cli/integration/hook_reintegration.py": ("hook_file_admitted=hook_file_admission(",),
    "src/apm_cli/commands/approve.py": ("narrowed = record_pins(allow, identity",),
    "src/apm_cli/install/native_plugin_admission.py": (
        "verdict = native_hooks_admission(allow_executables, keys",
        "hooks_approved = native_hook_approval(",
    ),
}
# A live-loaded plugin's hooks are admitted per file, never by a bool lookup.
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
)

__all__ = ["RULES"]
