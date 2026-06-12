"""External SARIF-native scanner ingestion seam for ``apm audit``.

This package implements a vendor-agnostic boundary between APM's own
:class:`~apm_cli.security.content_scanner.ContentScanner` and third-party,
SARIF-emitting skill/security scanners.  APM only *consumes* SARIF produced
by these tools and folds the findings into its existing report pipeline --
it never publishes anything back (one-directional, no partnership framing).

The whole capability is gated twice (fail-closed):

1. the ``external_scanners`` experimental flag must be enabled
   (``apm experimental enable external-scanners``), and
2. the SARIF source must be provided per run via CLI options --
   ``--external-sarif <file>`` for any tool, or a vendor CLI on ``PATH``
   (e.g. ``skillspector``).

Both opt-in steps are CLI-driven and install-method-neutral: they work the
same whether APM runs from source or as the self-contained binary (there is
no pip ``extra`` to install and no vendor Python package to import).

APM's native content scan always runs regardless of this seam; external
scanners only *add* findings, they never replace or weaken APM's own checks.
"""

from .base import ExternalScanError, ExternalScanner

__all__ = ["ExternalScanError", "ExternalScanner"]
