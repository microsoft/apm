"""TEMPORARY diagnostic plugin: replay a node order and probe the codex-pin victim."""

from __future__ import annotations

import importlib
import os
import tempfile
from pathlib import Path

import pytest

ORDER = [l for l in Path(os.environ["DIAG_ORDER"]).read_text(encoding="utf-8").splitlines() if l]
PROBE_FILES = set(filter(None, os.environ.get("DIAG_PROBE_EACH_IN", "").split(",")))
_last_of_file: set[str] = set()
for i, node in enumerate(ORDER):
    nxt = ORDER[i + 1] if i + 1 < len(ORDER) else None
    if nxt is None or nxt.split("::")[0] != node.split("::")[0]:
        _last_of_file.add(node)
_reported = False


def pytest_collection_modifyitems(session, config, items):
    by_id = {item.nodeid: item for item in items}
    missing = [n for n in ORDER if n not in by_id]
    print(f"DIAG ordered={len(ORDER)} matched={len(ORDER) - len(missing)} missing={len(missing)}", flush=True)
    items[:] = [by_id[n] for n in ORDER if n in by_id]


def _probe() -> tuple[bool, str]:
    from _pytest.monkeypatch import MonkeyPatch

    mod = importlib.import_module("tests.unit.install.test_cached_codex_source_pin")
    mp = MonkeyPatch()
    d = Path(tempfile.mkdtemp())
    cwd = os.getcwd()
    try:
        mod.test_frozen_global_install_from_warm_cache_uses_locked_source_pin(
            d, mp, mod._LOCKED_SHA, mod._LOCKED_SHA, mod._LOCKED_SHA, mod._CLIENT_REACHED
        )
        return True, ""
    except AssertionError as exc:
        return False, str(exc)[:600]
    finally:
        mp.undo()
        os.chdir(cwd)


def pytest_runtest_logfinish(nodeid, location):
    global _reported
    if _reported:
        return
    file = nodeid.split("::")[0]
    if nodeid in _last_of_file or file in PROBE_FILES:
        ok, msg = _probe()
        if not ok:
            print(f"\nDIAG FIRST-FAIL-AFTER {nodeid}\n{msg}", flush=True)
            _reported = True
