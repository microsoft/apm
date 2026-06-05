"""Generic SARIF-native scanner adapter.

Proves the seam is vendor-agnostic: it ingests a pre-generated SARIF file
produced by *any* SARIF 2.1.0-emitting tool (CodeQL, Semgrep, a custom
linter, ...).  This is the smallest, safest external integration -- APM
reads a file the user already trusts and folds its findings in; it never
shells out to an unknown binary.

The SkillSpector adapter (which *invokes* a vendor CLI) lives in
:mod:`apm_cli.security.external.skillspector`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from ..content_scanner import ScanFinding
from .base import ExternalScanError

if TYPE_CHECKING:
    from .options import ScannerOptions


class GenericSarifAdapter:
    """Ingest a user-supplied SARIF file into the audit pipeline."""

    name = "sarif"

    def __init__(self, sarif_file: str | Path | None = None) -> None:
        """Store the SARIF file to ingest.

        Args:
            sarif_file: Path to a SARIF 2.1.0 document.  Required before
                :meth:`scan` is called; the audit command supplies it from
                the ``--external-sarif`` option.
        """
        self._sarif_file = Path(sarif_file) if sarif_file is not None else None

    def is_available(self, *, options: ScannerOptions | None = None) -> tuple[bool, str | None]:
        """Available iff a readable SARIF file path was provided.

        *options* is accepted for protocol uniformity but ignored: this
        adapter only reads a file and has no LLM mode or argv passthrough.
        """
        if self._sarif_file is None:
            return (
                False,
                "the 'sarif' external scanner requires --external-sarif <file>",
            )
        if not self._sarif_file.exists():
            return False, f"SARIF file not found: {self._sarif_file}"
        if self._sarif_file.is_dir():
            return False, f"--external-sarif must be a file, not a directory: {self._sarif_file}"
        return True, None

    def scan(
        self, paths: list[Path], *, options: ScannerOptions | None = None
    ) -> dict[str, list[ScanFinding]]:
        """Read and parse the configured SARIF file into findings.

        *paths* and *options* are accepted for protocol uniformity but
        ignored: a pre-generated SARIF file already encodes its own locations
        and this adapter has no configurable behaviour.
        """
        from .sarif_ingest import sarif_to_findings

        if self._sarif_file is None:
            raise ExternalScanError(
                "No SARIF file configured for the 'sarif' external scanner "
                "(pass --external-sarif <file>)."
            )
        try:
            raw = self._sarif_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise ExternalScanError(f"Could not read SARIF file: {exc}") from exc
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ExternalScanError(
                f"SARIF file is not valid JSON: {self._sarif_file} ({exc})"
            ) from exc

        return sarif_to_findings(document, tool_name=self.name)
