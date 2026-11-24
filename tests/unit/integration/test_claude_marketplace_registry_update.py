"""A registry another writer changes mid-update is re-merged once, else left alone (#5176).

PROMISE: P-OWN-claude-native-plugin-registration -- APM's registry write merges
one entry into the file as it is right before the replace: a change made
between read and write is merged in on one retry, and a file that keeps
changing is left with the other writer's bytes (source: #5184 review F3).
owner: native_plugins/claude_marketplaces.py _merge
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apm_cli.native_plugins import claude_marketplaces as registry

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-claude-native-plugin-registration")]


def _racing(monkeypatch: pytest.MonkeyPatch, path: Path, edits: list[dict]) -> None:
    """Another writer replaces *path* right after each of APM's first reads of it."""
    original = Path.read_bytes
    calls = {"n": 0}

    def read_bytes(target: Path) -> bytes:
        raw = original(target)
        if target == path:
            calls["n"] += 1
            if calls["n"] % 2 == 1 and edits:
                target.write_text(json.dumps(edits.pop(0)), encoding="utf-8")
        return raw

    monkeypatch.setattr(Path, "read_bytes", read_bytes)


def _add_apm(data: dict) -> bool:
    data["apm-p-0123abcd"] = {"source": {"source": "directory", "path": "/p"}}
    return True


def test_a_change_between_read_and_write_is_merged_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "known_marketplaces.json"
    path.write_text("{}", encoding="utf-8")
    _racing(monkeypatch, path, [{"user": {"source": {"source": "github"}}}])

    registry._merge(path, _add_apm)

    assert set(json.loads(path.read_text(encoding="utf-8"))) == {"user", "apm-p-0123abcd"}


def test_a_file_that_keeps_changing_keeps_the_other_writers_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "known_marketplaces.json"
    path.write_text("{}", encoding="utf-8")
    _racing(monkeypatch, path, [{"first": {}}, {"second": {}}])

    with pytest.raises(registry._Skip, match="kept changing while APM updated it"):
        registry._merge(path, _add_apm)

    assert json.loads(path.read_text(encoding="utf-8")) == {"second": {}}


def test_an_update_during_staging_is_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The comparison runs after the candidate is staged, right before the replace."""
    import tempfile

    path = tmp_path / "known_marketplaces.json"
    path.write_text("{}", encoding="utf-8")
    original = tempfile.mkstemp
    edits = [{"during-staging": {}}]

    def mkstemp(*args, **kwargs):
        staged = original(*args, **kwargs)
        if edits:
            path.write_text(json.dumps(edits.pop(0)), encoding="utf-8")
        return staged

    monkeypatch.setattr(tempfile, "mkstemp", mkstemp)

    registry._merge(path, _add_apm)

    assert set(json.loads(path.read_text(encoding="utf-8"))) == {
        "during-staging",
        "apm-p-0123abcd",
    }
    assert sorted(p.name for p in tmp_path.iterdir()) == ["known_marketplaces.json"]
