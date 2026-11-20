"""Portable executable-trust guards (#3902, #4018).

Three owners keep "may this dependency run code here?" single-sourced:

* item admission  -- ``security/trust.py`` alone reads pinned item grants,
  and across the whole inventory no adapter looks approvals up itself,
  treats a missing approvals map as authorization, or omits an approval
  input (closed world, not a caller list);
* fingerprint     -- ``security/trust_fingerprint.py`` alone hashes;
* MCP admission   -- every ``CurrentMcpConfigView.derive`` call passes the
  owner's ``admission=``; no command passes a literal trust boolean.
"""

from __future__ import annotations

import ast
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
    # The one grant writer outside security/ pins what it approves.
    "src/apm_cli/commands/approve.py": ("narrowed = record_pins(allow, identity",),
}
# Type-level resolvers cannot see pins; outside security/ every per-type
# verdict comes from trust.package_type_verdicts.
_PIN_BLIND = ("resolve_exec_decision(", "exec_status_for_declaration(")
_SECURITY = "src/apm_cli/security/"
# Approval lookups belong to the trust owners; an adapter asks
# package_type_approved(s) or the item-admission owner instead.
_APPROVAL_LOOKUPS = (
    "is_package_approved",
    "is_any_type_approved",
    "_map_grants(",
    "_map_denies(",
    "allow_executables.get(",
)
# A missing approvals map never implies authorization (exec_map_authorizes_all).
_MAP_NONE_TEST = re.compile(
    r"\b(?:allow_executables|allow_map|allow_execs|effective_allow_execs|"
    r"effective_allow_executables|exec_allow_map)\s+is\s+(?:not\s+)?None\b"
)
# Approval inputs are required keyword-only arguments, passed at every call.
_APPROVAL_INPUTS = ("hooks_approved", "canvas_approved")
_TRUST_BIN_INPUT = "non_interactive"
_HOOK_ITEMS = "hook_file_admitted"
# Package-level approvals never skip native or pinned-hook item admission.
_PACKAGE_APPROVALS = "package_type_approvals"
_ITEM_ADMISSION_CALLS = ("native_install", "hooks_package_admissible")
# Native plugin status is decided over the native union of types.
_NATIVE_STATUS = "package_exec_status"
# Grants are persisted only by the approve command and the install prompt.
_GRANT_WRITERS = ("write_project_executables", "save_user_executables")
_GRANT_WRITER_PATHS = ("src/apm_cli/commands/approve.py", "src/apm_cli/install/phases/integrate.py")
_AST_PREFILTER = (
    *_APPROVAL_INPUTS,
    "trust_bin",
    _PACKAGE_APPROVALS,
    _NATIVE_STATUS,
    *_GRANT_WRITERS,
)

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
        "return compute_native_plugin_hash(plugin_root)",
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


def _code_lines(facts: object) -> list[str]:
    return [line for line in _lines(facts) if not line.lstrip().startswith(("#", "def "))]


def _callee(node: ast.Call, aliases: dict[str, str]) -> str | None:
    func = node.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    return aliases.get(name, name) if name else None


def _approval_signatures(trees: dict[str, ast.AST]) -> dict[str, frozenset[str]]:
    """Map every inventory function that takes an approval input to those inputs."""
    signatures: dict[str, frozenset[str]] = {}
    for tree in trees.values():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            names = {arg.arg for arg in (*node.args.posonlyargs, *node.args.args)}
            names |= {arg.arg for arg in node.args.kwonlyargs}
            inputs = {name for name in _APPROVAL_INPUTS if name in names}
            if _TRUST_BIN_INPUT in names and "trust_bin" in names:
                inputs.add(_TRUST_BIN_INPUT)
            if inputs:
                if _HOOK_ITEMS in names:
                    inputs.add(_HOOK_ITEMS)
                signatures[node.name] = signatures.get(node.name, frozenset()) | frozenset(inputs)
    return signatures


def _approval_input_findings(path: str, tree: ast.AST, signatures) -> list[Violation]:
    """Approval inputs are required keyword-only arguments, passed at every call."""
    findings: list[Violation] = []
    aliases = {
        alias.asname: alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.asname
    }
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            inputs = set(signatures.get(node.name, ())) - {_HOOK_ITEMS}
            required = {
                arg.arg
                for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True)
                if default is None
            }
            loose = sorted(name for name in inputs if name not in required)
            if loose:
                findings.append(
                    _summary(
                        GUARD_ITEM_ADMISSION,
                        path,
                        f"{node.name}: approval inputs must be required keyword-only "
                        f"arguments without a default: {', '.join(loose)}",
                    )
                )
        elif isinstance(node, ast.Call):
            name = _callee(node, aliases)
            inputs = signatures.get(name or "")
            if not inputs or any(keyword.arg is None for keyword in node.keywords):
                continue
            passed = {keyword.arg: keyword.value for keyword in node.keywords}
            missing = sorted(name for name in inputs - {_HOOK_ITEMS} if name not in passed)
            hooks = passed.get("hooks_approved")
            literal_false = isinstance(hooks, ast.Constant) and hooks.value is False
            if _HOOK_ITEMS in inputs and hooks is not None and not literal_false:
                missing += [] if _HOOK_ITEMS in passed else [_HOOK_ITEMS]
            if missing:
                findings.append(
                    _summary(
                        GUARD_ITEM_ADMISSION,
                        path,
                        f"Call to {name} at line {node.lineno} omits approval inputs: "
                        + ", ".join(missing),
                    )
                )
    return findings


def _routing_findings(path: str, tree: ast.AST) -> list[Violation]:
    """Adapters route package approvals and native status through item admission."""
    findings: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls = {_callee(item, {}) for item in ast.walk(node) if isinstance(item, ast.Call)} - {
            None
        }
        if _PACKAGE_APPROVALS in calls:
            skipped = [name for name in _ITEM_ADMISSION_CALLS if name not in calls]
            if skipped:
                findings.append(
                    _summary(
                        GUARD_ITEM_ADMISSION,
                        path,
                        f"{node.name}: package approvals bypass item admission; route through "
                        + ", ".join(skipped),
                    )
                )
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _callee(node, {})
        if name == _NATIVE_STATUS and not any(k.arg == "native" for k in node.keywords):
            findings.append(
                _summary(
                    GUARD_ITEM_ADMISSION,
                    path,
                    f"{_NATIVE_STATUS} at line {node.lineno} must pass native=",
                )
            )
        if name in _GRANT_WRITERS and path not in _GRANT_WRITER_PATHS:
            findings.append(
                _summary(
                    GUARD_ITEM_ADMISSION,
                    path,
                    f"Executable grants are written only by apm approve and the install "
                    f"prompt; remove {name}",
                )
            )
    return findings


def _inventory_findings(provider: FactsProvider) -> list[Violation]:
    """Closed-world approval checks over every inventory file outside security/."""
    findings: list[Violation] = []
    trees: dict[str, ast.AST] = {}
    for path in _python_paths(provider, _SRC):
        facts, failures = _facts_for(provider, path, GUARD_ITEM_ADMISSION)
        if failures:
            continue
        code = _code_lines(facts)
        if not path.startswith(_SECURITY):
            found = [token for token in _APPROVAL_LOOKUPS if any(token in line for line in code)]
            if any(_MAP_NONE_TEST.search(line) for line in code):
                found.append("an approvals map compared with None")
            if found:
                findings.append(
                    _summary(
                        GUARD_ITEM_ADMISSION,
                        path,
                        "Executable approval belongs to the trust owners "
                        "(package_type_approved, exec_map_authorizes_all); remove "
                        + ", ".join(found),
                    )
                )
        if any(token in line for line in code for token in _AST_PREFILTER):
            index = provider.tree_index(path)
            if index is not None and index.root is not None:
                trees[path] = index.root
    signatures = _approval_signatures(trees)
    for path, tree in trees.items():
        findings.extend(_approval_input_findings(path, tree, signatures))
        if not path.startswith(_SECURITY):
            findings.extend(_routing_findings(path, tree))
    return findings


def check_item_admission(provider: FactsProvider) -> tuple[Violation, ...]:
    """Pinned item grants are read only by security/trust.py."""
    findings = _missing(provider, GUARD_ITEM_ADMISSION, _ITEM_ROUTES)
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
    findings.extend(_inventory_findings(provider))
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
