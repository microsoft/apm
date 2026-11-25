"""Journal MCP client config writes at the deploy write seam (#4594, #4863).

MCP adapters write their native config files themselves. Each write is
bracketed: :func:`journal_config_write` creates the file's directory through
the seam and records the file (and the bytes APM is about to write) before
it changes, :func:`journal_config_written` reports
the finished write, so the install transaction can restore or keep the file.
Outside an install both record nothing.
"""

from __future__ import annotations

from pathlib import Path


def journal_config_write(config_path: Path, text: str, *, lf: bool = False) -> None:
    """Create an MCP config file's directory and record the file before APM writes it (#4594).

    The install transaction journals both at the deploy write seam, so a
    failed install restores the file. The directory is created through the
    seam, which seals its mode: replay keeps it when a user changes it
    (#5086). Outside an install this only creates the directory. *lf* says
    the writer turns CRLF into LF (a normalizing ``atomic_write_text``).
    """
    from ...integration.write_seam import mkdir, record_before_write
    from ...utils.content_hash import text_write_hash, text_write_size

    mkdir(config_path.parent, parents=True, exist_ok=True)
    # *text* is what APM is about to write: replay keeps any other bytes (#4863).
    record_before_write(
        config_path,
        follow=True,
        expected=text_write_hash(text, lf=lf),
        size=text_write_size(text, lf=lf),
    )


def journal_config_written(config_path: Path) -> None:
    """Report that APM finished writing an MCP config file, so the journal seals it (#4863)."""
    from ...integration.write_seam import record_after_write

    record_after_write(config_path, follow=True)
