"""Policy ``mcp.transport.allow`` evaluates canonical effective transports.

Guard ``install-deployment-policy-mcp-transport-projection`` (#3764): the
policy layer never reads a raw MCP transport spelling. The parser
canonicalizes the allow list once, and check 9 receives the effective
canonical transports from ``integration/mcp_transport_resolution.py``,
which projects server info through the ``mcp_transport.py`` owner.
"""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.install_policy_shared import (
    _banned,
    _configured,
    _require_all,
    _tree_python_paths,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Violation

RULE_POLICY_MCP_TRANSPORT = "install-deployment-policy-mcp-transport-projection"

_POLICY_TREE = "src/apm_cli/policy/"
_POLICY_PARSER = "src/apm_cli/policy/parser.py"
_POLICY_CHECKS = "src/apm_cli/policy/policy_checks.py"
_RESOLVER = "src/apm_cli/integration/mcp_transport_resolution.py"
_MCP_INSTALL = "src/apm_cli/integration/mcp_integrator_install.py"
_MCP_TRANSPORT = "src/apm_cli/mcp_transport.py"

# A raw transport read: a dependency/server ``.transport`` attribute (the
# policy schema's ``<policy>.transport.allow`` is the only allowed shape) or
# the legacy registry ``transport_type`` key.
_RAW_TRANSPORT_ATTR = re.compile(r"\.transport\b(?!\.allow\b)|transport_type")
# Raw ``transport`` key reads belong only to the policy schema parser.
_RAW_TRANSPORT_KEY = re.compile(r"""\.get\(\s*["']transport["']|\[\s*["']transport["']\s*\]""")

_REQUIRED: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        _POLICY_PARSER,
        (
            'allow=_parse_transport_allow(transport_data.get("allow"))',
            "return canonical_transport_names(",
        ),
        "mcp.transport.allow must be canonicalized once by the parser",
    ),
    (
        _POLICY_CHECKS,
        (
            "_check_mcp_transport(_effective_mcp_transports(mcp_list, policy.mcp), policy.mcp)",
            "from ..integration.mcp_transport_resolution import resolve_effective_mcp_transports",
        ),
        "check 9 must judge effective transports resolved by the owner",
    ),
    (
        _RESOLVER,
        (
            "server_write_transports(server_info, selected_remote=MCPClientAdapter.selected_remote)",
            "frozenset({canonical_transport_name(declared)})",
            "_overlaid_server_infos(",
            "MCPIntegrator._build_self_defined_info(",
        ),
        "effective MCP transports must project install's overlaid server info through mcp_transport.py",
    ),
    (
        _MCP_INSTALL,
        ("checked = policy_checked_registry_info(",),
        "install must write the registry snapshot org policy judged",
    ),
    (
        _MCP_TRANSPORT,
        ("return _ALIASES.get(token, token)", "transports.add(TRANSPORT_STDIO)"),
        "policy transport names and server projection must reuse the owner's alias table",
    ),
)


def check_policy_mcp_transport_projection(provider: FactsProvider) -> tuple[Violation, ...]:
    """Policy transport checks consume canonical values from the owner only."""
    rule_id = RULE_POLICY_MCP_TRANSPORT
    findings: list[Violation] = []
    for path, needles, message in _REQUIRED:
        lines, failures = _configured(provider, path, rule_id)
        findings.extend(failures)
        if not failures:
            findings.extend(_require_all(rule_id, path, lines, needles, message))
    findings.extend(
        _banned(
            provider,
            rule_id=rule_id,
            paths=_tree_python_paths(provider, _POLICY_TREE),
            pattern=_RAW_TRANSPORT_ATTR,
            message=(
                "Raw MCP transport read in policy/; take canonical effective transports "
                "from integration/mcp_transport_resolution.py"
            ),
            configured=False,
            respect_exempt=True,
        )
    )
    findings.extend(
        _banned(
            provider,
            rule_id=rule_id,
            paths=_tree_python_paths(provider, _POLICY_TREE, excluded=(_POLICY_PARSER,)),
            pattern=_RAW_TRANSPORT_KEY,
            message="Raw 'transport' key read in policy/; only the policy parser reads it",
            configured=False,
            respect_exempt=True,
        )
    )
    return tuple(findings)
