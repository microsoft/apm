"""Shared helpers for CompilationFormatter color-path tests.

Centralises the Rich-aware formatter construction so the two formatter
test modules cannot drift on Console width or other settings that affect
table-rendering assertions.
"""

from __future__ import annotations

import unittest

from apm_cli.output.formatters import CompilationFormatter


def make_color_formatter() -> CompilationFormatter:
    """Return a CompilationFormatter with use_color=True and a pinned width.

    Pins ``Console(width=200)`` so Rich does not shrink table columns under
    narrow CI terminal widths (Windows runners default to ~80 cols, which
    truncates content like "constitution" into "constitutio...").

    Raises:
        unittest.SkipTest: if Rich is not installed in the test environment.
    """
    f = CompilationFormatter(use_color=True)
    if not f.use_color:
        raise unittest.SkipTest("Rich not available")
    from rich.console import Console

    f.console = Console(width=200, force_terminal=False)
    return f
