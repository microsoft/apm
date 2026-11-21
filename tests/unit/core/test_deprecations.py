"""The deprecation catalog's metadata stays consistent with its own text (#4019)."""

from __future__ import annotations

import re
import string
from pathlib import Path

import pytest

from apm_cli.core.deprecations import (
    _CATALOG,
    DEPRECATIONS,
    SIGNAL_COLLECTED,
    SIGNAL_PYTHON,
    SIGNAL_WARNING,
    WARNING_FREE_DEPRECATIONS,
    deprecation_message,
)

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-user-facing-output-diagnostics")]

_SRC = Path(__file__).resolve().parents[3] / "src" / "apm_cli"
_VERSION = re.compile(r"\d+(\.\d+){1,2}")
_ANNOUNCED_VERSION = re.compile(r"[Rr]emov(?:ed|al) in v(\d+\.\d+(?:\.\d+)?)\b")


def test_keys_are_unique() -> None:
    assert len(DEPRECATIONS) == len(_CATALOG)


@pytest.mark.parametrize("entry", _CATALOG, ids=lambda entry: entry.key)
def test_entry_metadata_is_well_formed(entry) -> None:
    assert entry.signal in {SIGNAL_WARNING, SIGNAL_PYTHON, SIGNAL_COLLECTED}
    assert entry.since is None or _VERSION.fullmatch(entry.since)
    for text in (entry.surface, entry.replacement, entry.template):
        assert text.isascii() and text.strip() == text and text


@pytest.mark.parametrize("entry", _CATALOG, ids=lambda entry: entry.key)
def test_removal_horizon_matches_what_the_warning_announces(entry) -> None:
    """A horizon is recorded only when the text already names it; none is inferred."""
    announced = {match.group(1) for match in _ANNOUNCED_VERSION.finditer(entry.template)}

    assert announced == ({entry.removal} if entry.removal else set())


@pytest.mark.parametrize("entry", _CATALOG, ids=lambda entry: entry.key)
def test_every_entry_is_read_by_a_domain_owner(entry) -> None:
    fields = {name for _lit, name, _spec, _conv in string.Formatter().parse(entry.template) if name}
    readers = [
        path
        for path in _SRC.rglob("*.py")
        if path.name != "deprecations.py"
        and re.search(
            rf"""deprecation_message\(\s*["']{re.escape(entry.key)}["']""", path.read_text("utf-8")
        )
    ]

    assert readers, f"{entry.key} is never emitted"
    assert deprecation_message(entry.key, **dict.fromkeys(fields, "X")).isascii()


def test_warning_free_inventory_points_at_existing_modules() -> None:
    root = _SRC.parents[1]
    for item in WARNING_FREE_DEPRECATIONS:
        assert (root / item.location).is_file(), item.location
        assert item.kind in {"flag", "file", "python-api"}
        assert item.surface not in {entry.surface for entry in _CATALOG}
