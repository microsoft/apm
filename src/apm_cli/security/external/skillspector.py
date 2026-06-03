"""NVIDIA SkillSpector adapter.

Invokes the SkillSpector CLI -- when it is resolvable on ``PATH`` -- over the
requested paths, asks it for SARIF output, and folds the findings into the
audit pipeline.  The vendor CLI is located lazily via ``shutil.which`` and
never imported as a Python package, so this adapter works identically whether
APM runs from source or as the self-contained PyInstaller binary.

Users who cannot install the SkillSpector CLI can instead emit a SARIF file
from any tool and ingest it with ``--external sarif --external-sarif <file>``.

APM only consumes SkillSpector's SARIF; it publishes nothing back
(one-directional, no partnership framing).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from ..content_scanner import ScanFinding
from .base import ExternalScanError

#: Executable name expected on PATH when the SkillSpector CLI is installed.
_BINARY = "skillspector"

#: Bounded wall-clock budget so a hung vendor process can't stall the audit.
_TIMEOUT_SECONDS = 300


class SkillSpectorAdapter:
    """Run NVIDIA SkillSpector and ingest its SARIF output."""

    name = "skillspector"

    def is_available(self) -> tuple[bool, str | None]:
        """Available iff the SkillSpector binary is resolvable on PATH."""
        if shutil.which(_BINARY) is None:
            return (
                False,
                "SkillSpector CLI not found on PATH. Install the 'skillspector' "
                "tool, or use '--external sarif --external-sarif <file>' to ingest "
                "a SARIF file from any scanner (works with the APM binary).",
            )
        return True, None

    def scan(self, paths: list[Path]) -> dict[str, list[ScanFinding]]:
        """Invoke SkillSpector over *paths* and parse its SARIF output."""
        from .sarif_ingest import sarif_to_findings

        binary = shutil.which(_BINARY)
        if binary is None:
            raise ExternalScanError(
                "SkillSpector CLI not found on PATH. Install the 'skillspector' "
                "tool, or use '--external sarif --external-sarif <file>'."
            )

        targets = [str(p) for p in paths] or ["."]
        cmd = [binary, "scan", "--format", "sarif", "--no-llm", *targets]
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ExternalScanError(f"SkillSpector timed out after {_TIMEOUT_SECONDS}s.") from exc
        except OSError as exc:
            raise ExternalScanError(f"Could not launch SkillSpector: {exc}") from exc

        if not completed.stdout.strip():
            # Non-zero exit with no SARIF is a tool error, not findings.
            detail = (completed.stderr or "").strip() or f"exit code {completed.returncode}"
            raise ExternalScanError(f"SkillSpector produced no SARIF output ({detail}).")

        try:
            document = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            # SkillSpector writes errors (e.g. missing API key) to stdout,
            # not stderr.  Surface the first line so users can diagnose.
            # Strip non-printable / non-ASCII chars to honour the repo's
            # printable-ASCII output contract.
            raw_line = completed.stdout.strip().splitlines()[0][:200]
            safe_line = "".join(ch if 0x20 <= ord(ch) <= 0x7E else "?" for ch in raw_line)
            raise ExternalScanError(
                f"SkillSpector output is not valid JSON SARIF: {safe_line}"
            ) from exc

        return sarif_to_findings(document, tool_name=self.name)
