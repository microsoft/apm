"""Apply an approved manifest delta, never source or deployment content."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from apm_cli.install.locking import lifecycle_operation

from .discovery import discover
from .manifest_edit import read_manifest, write_manifest


def apply_report(report: dict[str, Any]) -> dict[str, Any]:
    """Revalidate the consented plan under the normal lifecycle lock."""
    if any(item["status"] == "unsafe" for item in report["findings"]):
        raise ValueError(
            "Unsafe paths or install identity collisions found. Repair them and rediscover."
        )
    if not report["additions"]:
        return report
    root = Path(report["root"])
    manifest = Path(report["manifest"])
    with lifecycle_operation():
        data, _, original = read_manifest(manifest, root)
        current = discover(root, manifest, user_scope=report["scope"] == "global")
        if current != report:
            raise ValueError(
                "Discovery changed since the preview. Review the new report and retry."
            )
        current["applied"] = write_manifest(manifest, root, data, current["additions"], original)
    return current
