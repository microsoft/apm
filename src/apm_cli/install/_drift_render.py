"""Drift-result rendering helpers (text / JSON / SARIF).

Extracted from ``drift.py`` to keep that module under the file-length gate.
Import the public symbols via ``apm_cli.install.drift`` (which re-exports them)
rather than directly from this module.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from apm_cli.utils.console import STATUS_SYMBOLS

if TYPE_CHECKING:
    from .drift import DriftFinding


def render_drift_text(findings: list[DriftFinding], verbose: bool = False) -> str:
    """Human-readable text rendering grouped by kind."""
    if not findings:
        return f"{STATUS_SYMBOLS['check']} No drift detected"

    lines: list[str] = [
        f"{STATUS_SYMBOLS['warning']} Drift detected: {len(findings)} file(s)",
        "",
    ]
    by_kind: dict[str, list[DriftFinding]] = {}
    for f in findings:
        by_kind.setdefault(f.kind, []).append(f)

    for kind in ("modified", "unintegrated", "orphaned"):
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

    lines.append(
        f"  {STATUS_SYMBOLS['info']} Run 'apm install' to re-sync deployed files with the lockfile."
    )

    return "\n".join(lines).rstrip() + "\n"


def render_drift_json(findings: list[DriftFinding]) -> dict:
    """Machine-readable JSON shape: ``{\"drift\": [...]}``."""
    return {
        "drift": [
            {
                "path": f.path,
                "kind": f.kind,
                "package": f.package,
                "inline_diff": f.inline_diff,
            }
            for f in findings
        ]
    }


def render_drift_sarif(findings: list[DriftFinding]) -> list[dict]:
    """SARIF ``results`` array; rule IDs use ``apm/drift/<kind>``."""
    results: list[dict] = []
    for f in findings:
        results.append(
            {
                "ruleId": f"apm/drift/{f.kind}",
                "level": "warning" if f.kind != "modified" else "error",
                "message": {"text": f"drift ({f.kind}): {f.path}"},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": f.path},
                        }
                    }
                ],
                "properties": {"package": f.package},
            }
        )
    return results


# ---------------------------------------------------------------------------
# CLI helper -- intentionally minimal so commands/audit.py can re-use it.
# ---------------------------------------------------------------------------


def render_drift(
    findings: list[DriftFinding],
    fmt: str = "text",
    verbose: bool = False,
) -> str:
    """Single rendering entrypoint for callers that pick a format string."""
    if fmt == "json":
        return json.dumps(render_drift_json(findings), indent=2)
    if fmt == "sarif":
        return json.dumps({"results": render_drift_sarif(findings)}, indent=2)
    return render_drift_text(findings, verbose=verbose)
