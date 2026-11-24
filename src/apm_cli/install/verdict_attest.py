"""When a full install attests the state it committed (W3d attested-record ruling A2).

Kept out of ``commands/install.py`` (file length budget) and out of
``install/verdict_record.py``, whose imports stay light for the replay.
"""

from __future__ import annotations

from typing import Any


def attest_clean_install(result: Any, *, dry_run: bool) -> Any:
    """A full install that converged with nothing kept, skipped, refused or deferred
    attests its committed state, so the next unchanged install skips the proof."""
    from apm_cli.install import verdict_record
    from apm_cli.models.results import InstallDisposition

    # Every warning or error line the run printed, collector summaries included,
    # is judged by the record itself (W3d A2 allowlist, verdict_record._unsettled_line).
    if (
        not dry_run
        and result.exit_code == 0
        and getattr(result, "disposition", None) is InstallDisposition.SUCCESS
    ):
        verdict_record.note_attested_install()
    return result
