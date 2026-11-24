"""Throwaway probe: replay generated-graph seed 398718870 (not for merge)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.integration import test_prune_generated_local_graphs as g

pytestmark = [pytest.mark.integration, pytest.mark.promise("P-OWN-orphan-package-selection")]


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
@pytest.mark.parametrize("rep", range(3))
def test_seed_398718870_replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rep: int) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "s"
    root.mkdir()
    verdict, _ = g._check_seed(398718870, root, tally={})
    print(f"PROBE-VERDICT rep={rep}: {verdict[:1500] if verdict else verdict}")
    assert verdict in (None, "unchanged", "vacuous", "converged", "skipped"), verdict
