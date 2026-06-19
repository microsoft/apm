"""Protocol and shared types for external SARIF-native scanners.

Every concrete adapter (SkillSpector, generic SARIF, ...) implements the
:class:`ExternalScanner` protocol so the audit command can treat them
uniformly.  Adapters normalise vendor SARIF into APM's internal
:class:`~apm_cli.security.content_scanner.ScanFinding` value object via
:mod:`apm_cli.security.external.sarif_ingest`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..content_scanner import ScanFinding

if TYPE_CHECKING:
    from .options import ScannerOptions


class ExternalScanError(RuntimeError):
    """Raised when an external scanner cannot run or returns unusable output.

    Carries a user-facing, actionable message.  The audit command renders
    the message and exits non-zero rather than crashing with a traceback.
    """


@runtime_checkable
class ExternalScanner(Protocol):
    """A third-party SARIF-emitting scanner adapted into the audit pipeline.

    Implementations are thin: they obtain a SARIF document (by invoking a
    vendor CLI or by reading a user-supplied file) and delegate parsing to
    :func:`apm_cli.security.external.sarif_ingest.sarif_to_findings`.
    """

    #: Stable, lowercase identifier used on the ``--external`` CLI option.
    name: str

    def is_available(self, *, options: ScannerOptions | None = None) -> tuple[bool, str | None]:
        """Report whether this scanner can run in the current environment.

        Args:
            options: Resolved scanner options for this run. Adapters use them
                only when relevant (e.g. checking for an LLM API key when
                ``options.llm`` is set); options-unaware adapters ignore it.

        Returns:
            ``(True, None)`` when the scanner is usable, or
            ``(False, reason)`` where *reason* is a one-line, actionable
            message (e.g. "pass --external-sarif <file>" or "tool not on
            PATH"). Messages are install-method-neutral -- they never assume
            APM was installed via pip.
        """
        ...

    def scan(
        self, paths: list[Path], *, options: ScannerOptions | None = None
    ) -> dict[str, list[ScanFinding]]:
        """Run the scanner over *paths* and return findings grouped by file.

        Args:
            paths: Files or directories to scan.
            options: Resolved scanner options (LLM toggle, validated extra
                argv). ``None`` means adapter defaults. Adapters use only the
                options they understand.

        Returns:
            A ``{file: [ScanFinding, ...]}`` mapping ready to merge into the
            audit report's ``findings_by_file``.

        Raises:
            ExternalScanError: If the scanner fails or emits unusable output.
        """
        ...
