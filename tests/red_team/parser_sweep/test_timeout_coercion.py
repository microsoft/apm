"""RED-TEAM round-2: timeout coercion edges beyond round 1.

Round 1 exercised string / negative / bool / float / huge timeouts at the
SUBPROCESS firing boundary. This probe goes DEEPER into the coercion model
itself:

* the ``timeoutSec or timeout`` merge in ``_build_entry`` (0 must fall
  through to the per-type default, never to None / unbounded);
* ``effective_timeout`` for exotic values (NaN, list, str-number);
* the central question: can ANY manifest value make ``effective_timeout``
  return ``None`` (which subprocess interprets as *wait forever*)?

The contract: a command script is ALWAYS bounded. No parsed manifest value
may disable the timeout, and a bad value must be isolated by ``fire()``
(no escape, no hang).
"""

from __future__ import annotations

import math

import pytest

from .conftest import command_entry, fire, run_guarded


def _build(raw_timeout_sec, raw_timeout=None):
    """Run a raw entry through the real parser and return the ScriptEntry."""
    from pathlib import Path

    from apm_cli.core.lifecycle_scripts import _build_entry

    raw = {"type": "command", "command": "echo hi"}
    if raw_timeout_sec is not None:
        raw["timeoutSec"] = raw_timeout_sec
    if raw_timeout is not None:
        raw["timeout"] = raw_timeout
    return _build_entry(raw, "post-install", Path("apm.yml"), "project")


def test_timeoutsec_zero_falls_through_to_default():
    """timeoutSec: 0 is falsy -> default 30 (command), never 0 / unbounded."""
    entry = _build(0)
    assert entry.timeout_sec is None
    assert entry.effective_timeout == 30


def test_timeoutsec_zero_with_timeout_alias_uses_alias():
    """timeoutSec:0 falls through to the `timeout` alias when present."""
    entry = _build(0, raw_timeout=5)
    assert entry.effective_timeout == 5


def test_no_manifest_value_yields_none_timeout():
    """effective_timeout must never be None for a command script.

    None would mean subprocess waits forever. Sweep a spread of hostile
    raw values; none may collapse the bound to None.
    """
    for raw in [0, 0.0, False, "", [], {}, None]:
        entry = _build(raw)
        assert entry.effective_timeout is not None
        assert entry.effective_timeout == 30


def test_nan_timeout_does_not_disable_bound(tmp_path, fire_event):
    """A NaN timeout must not produce an unbounded run.

    YAML ``.nan`` parses to float('nan'). subprocess cannot convert NaN to
    an integer poll timeout, so it raises immediately -- the executor's
    isolation swallows it and the call returns fast. The danger we rule
    out: NaN silently meaning *no timeout* while a slow command runs.
    """
    entry = command_entry(bash="sleep 30", command="sleep 30", timeout_sec=float("nan"))
    assert math.isnan(entry.effective_timeout)

    finished, _result, exc = run_guarded(lambda: fire(entry, fire_event, tmp_path), timeout=6.0)
    assert finished, "NaN timeout disabled the bound -- slow command ran unbounded"
    assert exc is None, f"NaN timeout leaked an exception from fire(): {exc!r}"


@pytest.mark.parametrize("bad", [[1], {"a": 1}, "30", "1e9"])
def test_list_dict_strnumber_timeout_isolated(tmp_path, fire_event, bad):
    """list / dict / string-number timeouts must be isolated, not crash/hang."""
    entry = command_entry(timeout_sec=bad)
    finished, _result, exc = run_guarded(lambda: fire(entry, fire_event, tmp_path), timeout=6.0)
    assert finished, f"fire() hung with timeout_sec={bad!r}"
    assert exc is None, f"fire() leaked with timeout_sec={bad!r}: {exc!r}"


def test_string_number_timeout_is_not_coerced_to_int():
    """timeoutSec:'30' stays a str (no silent coercion) -> isolated at fire.

    Documents that the parser does NOT numeric-coerce string timeouts; the
    value passes through verbatim and is handled by executor isolation.
    """
    entry = _build("30")
    assert entry.effective_timeout == "30"
    assert not isinstance(entry.effective_timeout, int)
