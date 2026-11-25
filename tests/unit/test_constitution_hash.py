from __future__ import annotations

import pytest

from apm_cli.compilation.constitution_block import compute_constitution_hash

pytestmark = pytest.mark.unit


def test_hash_stable_and_truncated():
    data = "Line A\nLine B\n"
    h1 = compute_constitution_hash(data)
    h2 = compute_constitution_hash(data)
    assert h1 == h2
    assert len(h1) == 12


def test_hash_differs_on_change():
    h1 = compute_constitution_hash("X")
    h2 = compute_constitution_hash("X ")
    assert h1 != h2


def test_hash_empty():
    h = compute_constitution_hash("")
    assert len(h) == 12
