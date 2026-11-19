"""Keep pack-time ecosystem plugin.json generation in ``core/plugin_manifest.py``.

``apm pack`` maps each ``targets:`` ecosystem (claude, copilot, codex) to one
generated ``plugin.json`` path and shape. Producers, bundle exporters and the
Codex compatibility reader route through ``PLUGIN_ECOSYSTEM_PATHS`` and
``build_plugin_manifest`` instead of re-declaring host manifest paths (#3077).
"""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.marketplace_integration_shared import (
    GROUP,
    _forbid_scan,
    _paths_under,
    _src_python,
)
from scripts.architecture_linter.checks.marketplace_package_and_registration import (
    _count_across,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Rule, Violation

RULE_ID = "marketplace-integrations-pack-plugin-manifests"
_OWNER = "src/apm_cli/core/plugin_manifest.py"
_PACK_SURFACE_PREFIXES = ("src/apm_cli/core/", "src/apm_cli/bundle/", "src/apm_cli/codex_plugins/")
_PACK_COMMAND = "src/apm_cli/commands/pack.py"
_OWNER_DEFINITIONS = (
    (re.compile(r"^PLUGIN_ECOSYSTEM_PATHS\b"), "PLUGIN_ECOSYSTEM_PATHS"),
    (re.compile(r"^def build_plugin_manifest\("), "build_plugin_manifest"),
)
_HOST_MANIFEST_LITERAL = re.compile(
    r"[\"'](?:\.claude-plugin|\.codex-plugin|\.github/plugin)(?:/plugin\.json)?[\"']"
)
_CODEX_NAME_RULE_LITERAL = re.compile(re.escape("[A-Za-z0-9][A-Za-z0-9._-]*"))
# Pack never ships the .mcp.json servers APM itself rendered (#3966): reading a
# root .mcp.json excludes APM-managed servers by default, and the only opt-out
# is a dependency's own authored file.
_MCP_OPT_OUT = "exclude_apm_managed=False"
_MCP_OPT_OUT_SITES = {("src/apm_cli/bundle/plugin_exporter.py", "_cache_would_contribute_hooks_or_mcp")}
_MCP_DEFAULT = "exclude_apm_managed: bool = True"


def _check(provider: FactsProvider) -> tuple[Violation, ...]:
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []
    sources = _src_python(provider)
    for pattern, name in _OWNER_DEFINITIONS:
        total = _count_across(provider, inv, RULE_ID, sources, pattern)
        owned = _count_across(provider, inv, RULE_ID, (_OWNER,), pattern)
        if total != 1 or owned != 1:
            findings.append(
                violation(RULE_ID, _OWNER, f"{name} must be defined exactly once, in the owner")
            )
    surface = [
        path
        for prefix in _PACK_SURFACE_PREFIXES
        for path in _paths_under(provider, prefix, (".py",))
        if path != _OWNER
    ]
    surface.append(_PACK_COMMAND)
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            RULE_ID,
            tuple(surface),
            _HOST_MANIFEST_LITERAL,
            "pack-time host plugin.json paths must route through PLUGIN_ECOSYSTEM_PATHS",
            exempt=False,
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            RULE_ID,
            tuple(surface),
            _CODEX_NAME_RULE_LITERAL,
            "Codex plugin name rule must route through is_safe_codex_plugin_name",
            exempt=False,
        )
    )
    findings.extend(_mcp_ownership_findings(provider))
    return tuple(findings)


def _mcp_ownership_findings(provider: FactsProvider) -> list[Violation]:
    findings: list[Violation] = []
    owner_lines = getattr(provider.file_facts(_OWNER), "lines", ())
    if not any(_MCP_DEFAULT in line for line in owner_lines):
        findings.append(
            violation(RULE_ID, _OWNER, "collect_mcp_servers must exclude APM-managed servers by default")
        )
    for path in _src_python(provider):
        facts = provider.file_facts(path)
        lines = getattr(facts, "lines", ())
        scopes = {
            (d.line, d.end_line, d.name) for d in getattr(facts, "definitions", ()) if d.kind != "class"
        }
        for number, line in enumerate(lines, start=1):
            if _MCP_OPT_OUT not in line:
                continue
            enclosing = [s for s in scopes if s[0] <= number <= s[1]]
            name = min(enclosing, key=lambda s: s[1] - s[0])[2] if enclosing else ""
            if (path, name) not in _MCP_OPT_OUT_SITES:
                findings.append(
                    violation(
                        RULE_ID,
                        path,
                        "pack must not ship APM-rendered .mcp.json servers; only a dependency's "
                        "own .mcp.json may opt out of the ownership exclusion (#3966)",
                        line=number,
                    )
                )
    return findings


RULES = (
    Rule(
        id=RULE_ID,
        group=GROUP,
        guard_ids=(RULE_ID,),
        description="Pack-time ecosystem plugin.json paths and shapes route through core/plugin_manifest.py.",
        check=_check,
    ),
)


__all__ = ["RULES"]
