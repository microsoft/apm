"""Keep acquired-manifest metadata behind its snapshot and parsing owners."""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.install_deployment_shared import (
    _SRC_PREFIX,
    _duplicate_definition_lines,
    _facts_for,
    _present,
    _summary,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Violation

GUARD_MANIFEST_SNAPSHOT = "install-deployment-manifest-snapshot"
_SNAPSHOT = "src/apm_cli/install/source_helpers.py"
_SOURCES = "src/apm_cli/install/sources.py"


def check_manifest_snapshot(provider: FactsProvider) -> tuple[Violation, ...]:
    """Sources capture acquired metadata once through the construction owner."""
    rule = GUARD_MANIFEST_SNAPSHOT
    snapshot, snapshot_failures = _facts_for(provider, _SNAPSHOT, rule)
    sources, source_failures = _facts_for(provider, _SOURCES, rule)
    findings = list(snapshot_failures) + list(source_failures)
    if findings:
        return tuple(findings)

    findings.extend(
        _duplicate_definition_lines(
            provider,
            rule_id=rule,
            prefix=_SRC_PREFIX,
            pattern=re.compile(r"^[ \t]*def materialized_manifest_snapshot\("),
            owner=_SNAPSHOT,
            message="Acquired manifest evidence must use its canonical owner",
            respect_exempt=False,
        )
    )

    required_snapshot_calls = {"load_yaml", "APMPackage.from_mapping"}
    snapshot_calls = {
        call.qualname for call in snapshot.calls if call.scope == "materialized_manifest_snapshot"
    }
    required_consumers = {
        "LocalDependencySource",
        "CachedDependencySource",
        "FreshDependencySource",
    }
    consumers = {
        definition.scope
        for definition in sources.definitions
        if definition.name == "acquire"
        and any(
            call.qualname == "materialized_manifest_snapshot"
            and definition.line <= call.line <= definition.end_line
            for call in sources.calls
        )
    }
    if (
        not required_snapshot_calls.issubset(snapshot_calls)
        or "APMPackage.from_apm_yml" in snapshot_calls
        or not _present(snapshot, "source_path=package.source_path")
        or not _present(snapshot, "package_type == PackageType.MARKETPLACE_PLUGIN")
        or consumers != required_consumers
    ):
        findings.append(
            _summary(
                rule,
                _SNAPSHOT,
                "Materialization must capture current manifest metadata through from_mapping "
                "and preserve source anchors and native projections",
            )
        )
    return tuple(findings)
