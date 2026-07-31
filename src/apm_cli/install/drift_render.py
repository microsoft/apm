"""Output renderers for install-replay drift findings."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from apm_cli.utils.console import STATUS_SYMBOLS

if TYPE_CHECKING:
    from apm_cli.install.drift import DriftFinding


def render_drift_text(findings: list[DriftFinding], verbose: bool = False) -> str:
    """Render drift findings as grouped terminal text."""
    if not findings:
        return f"{STATUS_SYMBOLS['check']} No drift detected"

    status = (
        "error"
        if any(finding.kind in {"modified", "unrecorded"} for finding in findings)
        else "warning"
    )
    lines = [f"{STATUS_SYMBOLS[status]} Drift detected: {len(findings)} file(s)", ""]
    by_kind: dict[str, list[DriftFinding]] = {}
    for finding in findings:
        by_kind.setdefault(finding.kind, []).append(finding)

    for kind in ("modified", "unintegrated", "orphaned", "unrecorded"):
        items = by_kind.get(kind, [])
        if not items:
            continue
        lines.append(f"  {kind} ({len(items)}):")
        for item in items:
            suffix = f"  [{item.package}]" if item.package else ""
            lines.append(f"    - {item.path}{suffix}")
            if verbose and item.inline_diff:
                lines.append(f"      {item.inline_diff}")
        lines.append("")

    if by_kind.get("unrecorded"):
        lines.append(f"  {STATUS_SYMBOLS['info']} 1. Run 'apm install' to re-sync deployed files.")
        lines.append(
            f"  {STATUS_SYMBOLS['info']} 2. Commit the regenerated apm.lock.yaml "
            "to restore content-integrity coverage."
        )
    else:
        lines.append(
            f"  {STATUS_SYMBOLS['info']} Run 'apm install' to re-sync deployed files "
            "with the lockfile."
        )
    return "\n".join(lines).rstrip() + "\n"


def render_drift_json(findings: list[DriftFinding]) -> dict:
    """Render drift findings in the machine-readable JSON shape."""
    return {
        "drift": [
            {
                "path": finding.path,
                "kind": finding.kind,
                "package": finding.package,
                "inline_diff": finding.inline_diff,
            }
            for finding in findings
        ]
    }


def render_drift_sarif(findings: list[DriftFinding]) -> list[dict]:
    """Render the SARIF results array using ``apm/drift/<kind>`` rule IDs."""
    return [
        {
            "ruleId": f"apm/drift/{finding.kind}",
            "level": "error" if finding.kind in {"modified", "unrecorded"} else "warning",
            "message": {"text": f"drift ({finding.kind}): {finding.path}"},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": finding.path},
                    }
                }
            ],
            "properties": {"package": finding.package},
        }
        for finding in findings
    ]


def render_drift(
    findings: list[DriftFinding],
    fmt: str = "text",
    verbose: bool = False,
) -> str:
    """Render findings through the requested output format."""
    if fmt == "json":
        return json.dumps(render_drift_json(findings), indent=2)
    if fmt == "sarif":
        return json.dumps({"results": render_drift_sarif(findings)}, indent=2)
    return render_drift_text(findings, verbose=verbose)
