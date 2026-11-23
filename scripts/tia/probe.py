"""Test impact analysis S0 probe (#4508): per-test coverage of ``apm`` subprocesses.

Load with ``-p scripts.tia.probe`` under ``coverage run --rcfile scripts/tia/coveragerc``
(``patch = subprocess`` measures every child Python process). For each test this
plugin points ``COVERAGE_FILE`` at a per-test path in ``TIA_PROBE_DIR``, so a child
process started during the test writes its data there; the test's node id is
written next to it. In-process code is attributed by the ``test_function``
dynamic context. Nothing is selected or skipped.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from pathlib import Path

import pytest


def _probe_dir() -> Path | None:
    value = os.environ.get("TIA_PROBE_DIR")
    return Path(value) if value else None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None) -> Iterator[None]:
    root = _probe_dir()
    if root is None:
        yield
        return
    root.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(item.nodeid.encode("utf-8")).hexdigest()[:20]
    (root / f"{key}.node").write_text(item.nodeid, encoding="utf-8")
    previous = os.environ.get("COVERAGE_FILE")
    os.environ["COVERAGE_FILE"] = str(root / f"{key}.cov")
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("COVERAGE_FILE", None)
        else:
            os.environ["COVERAGE_FILE"] = previous
