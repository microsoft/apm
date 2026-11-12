"""Onboarding presentation at the CommandLogger boundary."""

from __future__ import annotations

import json
import sys
from typing import Any

from apm_cli.core.command_logger import CommandLogger
from apm_cli.utils.yaml_io import yaml_to_str


class DiscoveryLogger(CommandLogger):
    """Render inventory without exposing file contents or native secrets."""

    def report(self, report: dict[str, Any], fmt: str) -> None:
        """Render one complete report; machine stdout contains only data."""
        if fmt != "text":
            content = json.dumps(report, indent=2) + "\n" if fmt == "json" else yaml_to_str(report)
            sys.stdout.write(content)
            return
        self.info(f"Discovery ({report['scope']}): {report['root']}")
        for finding in report["findings"]:
            self.info(f"{finding['status']}: {finding['path']} -- {finding['reason']}")
        for entry in report["additions"]:
            self.info(f"apm.yml dependencies.apm: path: {entry['path']}")
        if report["applied"]:
            install = "apm install --global" if report["scope"] == "global" else "apm install"
            self.success(f"Updated consumer apm.yml. Run '{install}' separately.")
        elif report["additions"]:
            self.info(
                "Read-only preview. Use --apply to add these dependencies after confirmation."
            )
        else:
            self.info("No missing supported package dependencies; no manifest changes.")
