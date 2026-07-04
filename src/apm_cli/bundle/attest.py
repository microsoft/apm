"""Shared per-file provenance verification for the pack pipelines (#2013).

Both pack formats copy dependency files that ``apm install`` recorded in
``apm.lock.yaml`` under ``deployed_files`` + ``deployed_file_hashes``:

* ``--format plugin`` (:mod:`apm_cli.bundle.plugin_exporter`)
* ``--format apm``    (:mod:`apm_cli.bundle.packer`)

Before a byte enters a bundle its on-disk copy is verified against the
attested SHA-256 recorded at install time. A file that was tampered or
corrupted after ``apm install`` must never enter a bundle silently. Keeping
the check in ONE place gives both pack paths identical semantics and avoids a
copy-pasted block that would trip the pylint R0801 duplication gate.

Tolerance rule (forward-compat): a file with *no* recorded hash -- either an
older lockfile with an empty ``deployed_file_hashes`` or a specific path with
no recorded entry -- is packed without verification. Absence of an attestation
is tolerated; a *mismatched* attestation is a hard error. The unverified gap
is surfaced as a debug diagnostic so ``apm audit``-minded users can see it.
"""

import logging
from pathlib import Path

from ..utils.content_hash import compute_file_hash

_logger = logging.getLogger(__name__)


def verify_attested_file(
    source: Path,
    expected_hash: str | None,
    dep_label: str,
    rel_display: str,
) -> None:
    """Fail loudly when *source* diverges from its attested SHA-256.

    Args:
        source: On-disk file being packed.
        expected_hash: The ``"sha256:<hex>"`` recorded in
            ``deployed_file_hashes``, or ``None`` when no hash was recorded.
        dep_label: Dependency identifier (``repo_url``) for the error message.
        rel_display: Human-readable path shown in diagnostics / errors.

    Raises:
        ValueError: When a recorded hash exists and the on-disk content does
            not match it.
    """
    if not expected_hash:
        _logger.debug(
            "no attested hash for %r from %s; packed without integrity "
            "verification (older lockfile)",
            rel_display,
            dep_label,
        )
        return
    actual = compute_file_hash(source)
    if actual != expected_hash:
        raise ValueError(
            f"Cannot pack dependency {dep_label}: installed file {rel_display!r} "
            "does not match the hash recorded in apm.lock.yaml. The installed "
            "copy may be stale or tampered. Run 'apm install' to restore "
            "attested content, then pack again."
        )
