"""When a full install attests the state it committed (W3d attested-record ruling A2).

Kept out of ``commands/install.py`` (file length budget) and out of
``install/verdict_record.py``, whose imports stay light for the replay.
"""

from __future__ import annotations

from typing import Any


def attest_clean_install(result: Any, diagnostics: Any, *, dry_run: bool) -> Any:
    """A full install that converged with nothing kept, skipped, refused or deferred
    attests its committed state, so the next unchanged install skips the proof."""
    from apm_cli.install import verdict_record
    from apm_cli.models.results import InstallDisposition

    unsettled = sorted(_unsettled(diagnostics))
    if unsettled:
        verdict_record._trace(f"no attestation (diagnostics: {', '.join(unsettled)})")
    elif (
        not dry_run
        and result.exit_code == 0
        and getattr(result, "disposition", None) is InstallDisposition.SUCCESS
    ):
        verdict_record.note_attested_install()
    return result


def _unsettled(diagnostics: Any) -> set[str]:
    """Categories that say the committed state may not be the planner's fixed point:
    a skipped or overwritten file, drift, an error, a security, policy or auth finding,
    or a lossy compilation. Warnings and info lines (parked MCP servers, hints) are
    re-printed by the live render of every attested hit."""
    from apm_cli.utils import diagnostics as owner

    blocking = {
        owner.CATEGORY_COLLISION,
        owner.CATEGORY_OVERWRITE,
        owner.CATEGORY_DRIFT,
        owner.CATEGORY_ERROR,
        owner.CATEGORY_SECURITY,
        owner.CATEGORY_POLICY,
        owner.CATEGORY_AUTH,
        owner.CATEGORY_AGENT_LOSSY_COMPILATION,
    }
    found = getattr(diagnostics, "_diagnostics", None) or ()
    return {d.category for d in found if d.category in blocking}
