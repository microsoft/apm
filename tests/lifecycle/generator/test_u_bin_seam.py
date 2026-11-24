"""U-BIN pin seam (#4894): a changed pinned element reinstates project bin cells.

The generator hashes the U-BIN elements (generator/bin_pin.py); a digest that
no longer matches ``rules.U_BIN_PIN`` makes every project-scope bin cell a
must-cover obligation. The seam is the digest function, so this unit replaces
it and leaves ``rules.classify`` real.
"""

from __future__ import annotations

import pytest

from tests.lifecycle.generator import bin_pin
from tests.lifecycle.generator.rules import Verdict, classify

pytestmark = [
    pytest.mark.unit,
    pytest.mark.promise("P-OWN-executable-item-admission"),
]

_PROJECT_BIN = {"PRIM": "bin", "SCOPE": "project"}


def test_a_matching_pin_keeps_project_bin_cells_plain_obligations() -> None:
    assert classify("pair", _PROJECT_BIN) == Verdict("obligation", ())


def test_a_changed_pin_makes_the_project_bin_cells_must_cover(monkeypatch) -> None:
    monkeypatch.setattr(bin_pin, "predicate_digest", lambda *_: "0" * 64)
    assert classify("pair", _PROJECT_BIN) == Verdict("obligation", ("U-BIN",), must_cover=True)
