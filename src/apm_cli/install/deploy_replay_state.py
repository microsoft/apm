"""Whether a journaled path still holds only what APM put there (#4863, #5087).

Read by :func:`apm_cli.install.deploy_snapshot._decide` before replay restores
a unit: nothing, its prior state, the bytes APM sealed, or a write APM began
and never sealed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from apm_cli.install.deploy_state import ABSENT as _ABSENT
from apm_cli.install.deploy_state import ensure_listable as _ensure_listable
from apm_cli.install.deploy_state import entry_matches_expected as _entry_matches_expected
from apm_cli.install.deploy_state import interrupted_staging as _interrupted_staging
from apm_cli.install.deploy_state import path_state as _state

if TYPE_CHECKING:
    from apm_cli.install.deploy_snapshot import _Replay


def _holds_apm_state(unit: _Replay) -> bool:
    """Whether the path holds only what APM put there (see :func:`_decide`)."""
    _ensure_listable(unit.path)
    current = _state(unit.path)
    whole = unit.events.get("")
    sealed = whole[2] if isinstance(whole, tuple) else whole
    if current in (_ABSENT, unit.prior, sealed):
        return True
    if isinstance(whole, tuple):
        was_dir = "dir" in unit.prior or (isinstance(sealed, dict) and "dir" in sealed)
        if whole[1] == _ABSENT_EXPECTED and "dir" in current and was_dir:
            # A removal of a directory killed partway leaves part of the tree (#5087).
            return _dir_holds_apm_state(unit, current, sealed)
        if not _matches_expected(current, whole[1]):
            return False
        if "dir" not in current or whole[1] is None:
            return True
    if "dir" not in current:
        return False
    return _dir_holds_apm_state(unit, current, sealed)


_ABSENT_EXPECTED = "absent"


def _matches_expected(current: dict[str, Any], expected: str | None) -> bool:
    """A write APM began and never sealed left *current* (``None``: cannot tell)."""
    if expected is None:
        return True
    if expected == "dir":
        return "dir" in current
    return current.get("file") == expected


def _dir_holds_apm_state(unit: _Replay, current: Mapping[str, Any], whole: Any) -> bool:
    """The directory's own mode and every entry below it are prior or APM's."""
    prior = unit.prior.get("dir", {})
    sealed = whole.get("dir", {}) if isinstance(whole, dict) else {}
    modes = {unit.prior.get("mode"), whole.get("mode") if isinstance(whole, dict) else None}
    if modes != {None} and current.get("mode") not in modes:
        return False
    entries: Mapping[str, str] = current["dir"]
    known = {*prior, *sealed, *unit.events}
    accounted, unknown = _interrupted_staging(unit.events, entries, known, unit.path), set()
    for rel, fingerprint in entries.items():
        event = unit.events.get(rel)
        pending = isinstance(event, tuple)
        if fingerprint in (prior.get(rel), sealed.get(rel), event[2] if pending else event) or (
            pending and _entry_matches_expected(fingerprint, event[1])
        ):
            accounted.add(rel)
        elif rel not in prior and rel not in sealed and event is None:
            unknown.add(rel)
    # A directory with no evidence of its own is APM's when APM wrote below it.
    for rel in sorted(unknown, key=len, reverse=True):
        if entries[rel].startswith("dir:") and any(a.startswith(f"{rel}/") for a in accounted):
            accounted.add(rel)
    return accounted == set(entries)
