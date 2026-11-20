"""Compute an expensive, JSON-serializable value once per pytest session.

Under pytest-xdist every worker runs module-scoped fixtures again. For
fixtures whose result depends only on repository sources (for example an
architecture-linter baseline), the first worker computes the value and the
others read it from the session's shared base temp directory. This is the
file-lock pattern documented by pytest-xdist; it needs no extra dependency.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar

import pytest

T = TypeVar("T")

_LOCK_TIMEOUT_SECONDS = 1800.0


def sources_digest(root: Path, relative_roots: Iterable[str], extra: Iterable[str] = ()) -> str:
    """Hash the bytes of every file under the given roots, plus extra key parts."""
    digest = hashlib.sha256()
    for part in extra:
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    for relative in sorted(relative_roots):
        base = root / relative
        files = sorted(base.rglob("*")) if base.is_dir() else [base]
        for path in files:
            if path.is_file() and "__pycache__" not in path.parts:
                digest.update(path.relative_to(root).as_posix().encode("utf-8"))
                digest.update(b"\0")
                digest.update(path.read_bytes())
    return digest.hexdigest()[:20]


def _session_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    base = tmp_path_factory.getbasetemp()
    # Workers get basetemp/popen-gwN; the parent is shared by the session.
    return base.parent if os.environ.get("PYTEST_XDIST_WORKER") else base


@contextmanager
def _exclusive(lock: Path, timeout: float = _LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    deadline = time.monotonic() + timeout
    while True:
        try:
            handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic() > deadline:
                raise TimeoutError(f"timed out waiting for shared fixture lock {lock}") from None
            time.sleep(0.2)
    try:
        yield
    finally:
        os.close(handle)
        lock.unlink(missing_ok=True)


def shared_session_value(
    tmp_path_factory: pytest.TempPathFactory,
    name: str,
    key: str,
    compute: Callable[[], T],
) -> T:
    """Return compute() once per session and key, shared across xdist workers."""
    root = _session_root(tmp_path_factory)
    stem = f"shared-{name}-{key}"
    value_path = root / f"{stem}.json"
    with _exclusive(root / f"{stem}.lock"):
        if value_path.is_file():
            return json.loads(value_path.read_text(encoding="utf-8"))
        value = compute()
        temporary = root / f"{stem}.json.tmp"
        temporary.write_text(json.dumps(value), encoding="utf-8")
        os.replace(temporary, value_path)
        return value
