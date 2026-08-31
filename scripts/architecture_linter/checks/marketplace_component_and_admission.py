"""Producer-admission marketplace analyzer.

Ports ``marketplace-integrations-producer-admission`` --
``bundle/agent_plugin_exporter.py`` owns portable-surface admission before
output projection (legacy check_bundle_format_authority.sh producer gate).
"""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.marketplace_integration_shared import (
    GROUP,
    _count_calls_named,
    _count_checks,
    _forbid_scan,
    _load,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import FileFacts, Rule, Violation

_RID_PRODUCER = "marketplace-integrations-producer-admission"


_EXPORTER = "src/apm_cli/bundle/agent_plugin_exporter.py"


_EXPORTER_LOADER_DUP = re.compile(
    r"validate_(plugin_manifest|mcp_config|lsp_extension)_(document|file)"
)


def _first_line_index(facts: FileFacts, needle: str) -> int | None:
    for index, line in enumerate(facts.lines):
        if needle in line:
            return index
    return None


def _check_producer_admission(provider: FactsProvider) -> tuple[Violation, ...]:
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []

    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_PRODUCER,
            (_EXPORTER,),
            _EXPORTER_LOADER_DUP,
            "Agent Plugin producer must not duplicate canonical loader validation",
            exempt=False,
        )
    )
    findings.extend(
        _count_checks(
            provider,
            inv,
            _RID_PRODUCER,
            _EXPORTER,
            (("re", r"^def _require_portable_agent_plugin\(", 1, "eq"),),
            "producer must own _require_portable_agent_plugin",
        )
    )

    facts, failures = _load(provider, inv, _RID_PRODUCER, _EXPORTER, parse=True)
    if failures:
        return tuple(findings) + failures

    gate = _first_line_index(facts, "    _require_portable_agent_plugin(dropped_surfaces)")
    dry_run = _first_line_index(facts, "    if dry_run:")
    mkdir = _first_line_index(facts, "    output_dir.mkdir(")
    if gate is None or dry_run is None or mkdir is None or gate >= dry_run or gate >= mkdir:
        findings.append(
            violation(
                _RID_PRODUCER,
                _EXPORTER,
                "Agent Plugin portable-surface admission must fail before output projection",
            )
        )

    lf_writes = _count_calls_named(facts, "write_text_lf")
    if lf_writes != 3:
        findings.append(
            violation(
                _RID_PRODUCER,
                _EXPORTER,
                f"exporter must use exactly 3 write_text_lf calls, found {lf_writes}",
            )
        )
    if _count_calls_named(facts, "write_text") != 0:
        findings.append(
            violation(
                _RID_PRODUCER,
                _EXPORTER,
                "exporter must not call raw Path.write_text; use write_text_lf",
            )
        )
    return tuple(findings)


RULES: tuple[Rule, ...] = (
    Rule(
        id=_RID_PRODUCER,
        group=GROUP,
        guard_ids=(_RID_PRODUCER,),
        description="Agent Plugin producer admission gates before output projection.",
        check=_check_producer_admission,
    ),
)


COLLECTORS: tuple[object, ...] = ()


__all__ = ["COLLECTORS", "RULES"]
