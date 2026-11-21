"""Session-shared fixture values computed once across xdist workers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.utils import xdist_shared

pytestmark = pytest.mark.component


def _factory(base: Path) -> MagicMock:
    factory = MagicMock(spec=pytest.TempPathFactory)
    factory.getbasetemp.return_value = base
    return factory


def test_value_is_computed_once_per_key_and_shared_by_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def compute() -> list[str]:
        calls.append("x")
        return ["rule-a"]

    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    gw0 = _factory(tmp_path / "popen-gw0")
    gw1 = _factory(tmp_path / "popen-gw1")

    assert xdist_shared.shared_session_value(gw0, "baseline", "k1", compute) == ["rule-a"]
    assert xdist_shared.shared_session_value(gw1, "baseline", "k1", compute) == ["rule-a"]
    assert calls == ["x"]
    assert xdist_shared.shared_session_value(gw1, "baseline", "k2", compute) == ["rule-a"]
    assert calls == ["x", "x"]
    assert not list(tmp_path.glob("*.lock"))


def test_without_xdist_the_basetemp_itself_holds_the_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)

    xdist_shared.shared_session_value(_factory(tmp_path), "baseline", "k", lambda: [1])

    assert (tmp_path / "shared-baseline-k.json").read_text(encoding="utf-8") == "[1]"


def test_a_held_lock_times_out_instead_of_hanging(tmp_path: Path) -> None:
    lock = tmp_path / "held.lock"
    lock.write_text("", encoding="utf-8")

    with pytest.raises(TimeoutError, match="shared fixture lock"):
        with xdist_shared._exclusive(lock, timeout=0.3):
            pass


def test_sources_digest_changes_with_content_and_key(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    module = tmp_path / "pkg" / "mod.py"
    module.write_text("A = 1\n", encoding="utf-8")

    first = xdist_shared.sources_digest(tmp_path, ["pkg"], ["rule-a"])
    assert first == xdist_shared.sources_digest(tmp_path, ["pkg"], ["rule-a"])
    assert first != xdist_shared.sources_digest(tmp_path, ["pkg"], ["rule-b"])
    module.write_text("A = 2\n", encoding="utf-8")
    assert first != xdist_shared.sources_digest(tmp_path, ["pkg"], ["rule-a"])
