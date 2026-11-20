"""Regression trap: root help and light commands must not import heavy verbs.

``apm --help``, ``apm doctor --help``, and ``apm config get`` must not load
``install``, ``audit``, ``export_patch``, ``pack``, ``marketplace``, ``uninstall``, ``update``,
or ``prune``. Those modules load only when the matching verb is dispatched.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_lazy_stub_short_help_matches_resolved_command():
    from apm_cli.cli import _LAZY_COMMANDS, _LazyCommand

    mismatches: list[str] = []
    for name, module, attr, help_text in _LAZY_COMMANDS:
        stub = _LazyCommand(name, module, attr, help_text)
        real = stub.resolve()
        stub_short = stub.get_short_help_str(120)
        real_short = real.get_short_help_str(120)
        if stub_short != real_short:
            mismatches.append(f"{name}: stub={stub_short!r} real={real_short!r}")
    assert not mismatches, "Lazy stub short_help drifted from the real command: " + "; ".join(
        mismatches
    )
