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
    return tuple(findings)


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
