"""RESEARCH (perf R3): the string fast path agrees with pathlib on every input."""

from __future__ import annotations

import random
from pathlib import PurePosixPath, PureWindowsPath

import pytest

from apm_cli.utils import path_fast

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-read-only-lockfile-path")]

_PARTS = ["a", "A", "b", "B", "abc", "ABC", "x.md", "..", ".", "", "d:foo", "C:", "\u0130", "\u00df",
          "ss", "SS", "e\u0301", "\u00e9", " ", "a b", "~1", "RUNNER~1"]  # fmt: skip
_POSIX_ROOTS = ["/", "//", "///", "", "./"]
_WIN_ROOTS = [
    "C:\\",
    "c:\\",
    "C:/",
    "D:\\",
    "\\\\srv\\share\\",
    "//srv/share/",
    "\\",
    "C:",
    "",
    "\\\\?\\C:\\",
]


def _outcome(fn, a, b):
    try:
        result = fn(a, b)
    except Exception as exc:
        return ("raise", type(exc).__name__, str(exc))
    return ("ok", type(result).__name__, str(result), getattr(result, "parts", None))


def _random_path(rng, flavour, roots):
    root = rng.choice(roots)
    parts = [rng.choice(_PARTS) for _ in range(rng.randrange(5))]
    sep = "/" if flavour is PurePosixPath or rng.random() < 0.3 else "\\"
    return flavour(root + sep.join(parts))


@pytest.mark.parametrize(
    "flavour,roots", [(PurePosixPath, _POSIX_ROOTS), (PureWindowsPath, _WIN_ROOTS)]
)
def test_fast_containment_matches_pathlib(flavour, roots) -> None:
    rng = random.Random(1805)  # noqa: S311 -- seeded test data
    checked = fast = 0
    for _ in range(60000):
        a = _random_path(rng, flavour, roots)
        b = (
            _random_path(rng, flavour, roots)
            if rng.random() < 0.6
            else flavour(*a.parts[: rng.randrange(len(a.parts) + 1)] or ["."])
        )
        if rng.random() < 0.2:
            b = flavour(str(b).upper())
        for fast_fn, slow_fn in (
            (path_fast.relative_to, path_fast._orig_relative_to),
            (path_fast.is_relative_to, path_fast._orig_is_relative_to),
        ):
            assert _outcome(fast_fn, a, b) == _outcome(slow_fn, a, b), (a, b, fast_fn.__name__)
        checked += 1
        fast += path_fast._remainder(a, b) is not path_fast._FALLBACK
    assert fast > checked // 8, (fast, checked)  # the fast path is actually exercised
