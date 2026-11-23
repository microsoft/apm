"""MCP and LSP server names share one name rule (#4984).

* ``models/dependency/server_names.py`` owns the character allowlist and the
  reserved object keys (``__proto__``, ``constructor``, ``prototype``).
* ``MCPDependency.validate`` and ``LSPDependency.validate`` call
  ``validate_server_name``; the Claude and Codex native plugin routes call
  ``is_reserved_server_name`` before admission.
* No other module compiles its own copy of the server-name allowlist.
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

GUARD_SERVER_NAME_RULE = "install-deployment-server-name-rule"

_OWNER = "src/apm_cli/models/dependency/server_names.py"
_OWNER_NEEDLES = (
    "def validate_server_name(",
    'RESERVED_OBJECT_KEYS = frozenset({"__proto__", "constructor", "prototype"})',
    "return name.rstrip().lower() in RESERVED_OBJECT_KEYS",
)
_CONSUMERS = {
    "src/apm_cli/models/dependency/mcp.py": "validate_server_name(",
    "src/apm_cli/models/dependency/lsp.py": 'validate_server_name(self.name, "LSP")',
    # Native plugin routes hand server names to the harness without the models.
    "src/apm_cli/claude_plugins/admission.py": "is_reserved_server_name(",
    "src/apm_cli/codex_plugins/manifest.py": "is_reserved_server_name(",
}
_ALLOWLIST_LITERAL = "[a-zA-Z0-9._@/:=-]{0,127}"


def check_server_name_rule(provider: FactsProvider) -> tuple[Violation, ...]:
    """MCP and LSP server names are validated by one owner."""
    rule = GUARD_SERVER_NAME_RULE
    owner, failures = _facts_for(provider, _OWNER, rule)
    if failures:
        return tuple(failures)
    findings = [
        _summary(rule, _OWNER, f"The server-name owner lost: {needle}")
        for needle in _OWNER_NEEDLES
        if not _present(owner, needle)
    ]
    for path, needle in _CONSUMERS.items():
        facts, path_failures = _facts_for(provider, path, rule)
        findings.extend(path_failures)
        if not path_failures and not _present(facts, needle):
            findings.append(
                _summary(
                    rule, path, f"Server names must be validated by the owner; missing {needle}"
                )
            )
    for path in _python_paths(provider, "src/apm_cli/"):
        if path == _OWNER:
            continue
        facts, path_failures = _facts_for(provider, path, rule)
        if not path_failures and _present(facts, _ALLOWLIST_LITERAL):
            findings.append(
                _summary(
                    rule, path, "Defines its own server-name allowlist; call validate_server_name"
                )
            )
    return tuple(findings)
