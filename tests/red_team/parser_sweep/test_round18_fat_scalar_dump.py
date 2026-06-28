"""Round-18 parser break r18-parser-1: fat-scalar dump-amplification bomb.

The round-13 expansion-weight guard charged every node a FLAT weight of 1, so
it bounded the OCCURRENCE COUNT of the alias-expanded graph but not its BYTE
size. PyYAML's representer reports ``ignore_aliases() == True`` for ``str`` /
``int`` / ``float`` / ``bytes`` / ``bool``, so on the DUMP side a shared scalar
is NOT re-anchored -- its full text is re-emitted once PER alias occurrence.

A single ~50KB anchored scalar aliased ~150 times therefore composes as only
~150 nodes (far under the 5M node-count budget -> the round-13 guard PASSED it)
yet re-serializes to ~7.5MB; aliased tens of thousands of times it re-emits to
~GBs and hangs/OOMs the emitter. This is reachable PRE-TRUST: ``apm install`` /
``apm uninstall`` round-trips the project ``apm.yml`` through ``dump_yaml`` /
``yaml_to_str`` before any trust gate, so an untrusted clone could wedge the
installer in the YAML emitter.

The fix makes the leaf weight BYTE-AWARE (``_leaf_byte_cost`` charges each
scalar occurrence ``max(1, len(value))``), so the expansion budget now models
the real dump-amplification cost and the bomb fails closed as a
``yaml.YAMLError`` at PARSE -- before ``dump_yaml`` / ``yaml_to_str`` is ever
reached. A single large scalar referenced a handful of times stays well under
budget and still resolves + dumps.

These traps assert: the fat-scalar fanout fails closed FAST at ``load_yaml_str``
(and at the path-based ``load_yaml``); a daemon-thread watchdog confirms the
dump sinks are NEVER reached for the bomb; and the controls (a single large
scalar with a few refs, plus a benign manifest) still parse AND round-trip
through ``dump_yaml`` / ``yaml_to_str`` correctly (no regression).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
import yaml

from apm_cli.utils.yaml_io import dump_yaml, load_yaml, load_yaml_str, yaml_to_str

# The pre-fix path passes parse then re-emits ~GBs in the emitter (hang/OOM);
# the fix raises at parse in ~0.01s. A generous 5s ceiling cleanly separates
# "raised fast at parse" from "reached the amplifying emitter".
_FAST_CEILING_S = 5.0

# A fat (~50KB) scalar, FLAT-fanned to a modest occurrence count. The low
# occurrence count is the point: it is far under any node-COUNT budget, so only
# the BYTE-aware weight can catch it. Byte weight ~= 50000 * 150 = 7.5M > 5M.
_FAT_LEN = 50_000
_FANOUT = 150


def _fat_scalar_bomb() -> str:
    big = "A" * _FAT_LEN
    refs = ", ".join(["*big"] * _FANOUT)
    return f'name: rt\nversion: 1.0.0\nbig: &big "{big}"\nfan: [{refs}]\n'


def _legit_large_scalar() -> str:
    """A single large scalar referenced a few times -- under budget, must parse."""
    big = "B" * 4_000
    return f'name: rt\nversion: 1.0.0\nbig: &big "{big}"\na: *big\nb: *big\nc: *big\n'


def _benign_manifest() -> dict:
    return {
        "name": "rt",
        "version": "1.0.0",
        "lifecycle": {
            "post-install": [{"type": "command", "run": "echo hi"}],
        },
    }


def test_fat_scalar_bomb_fails_closed_fast_str() -> None:
    """The fat-scalar fanout raises yaml.YAMLError fast at load_yaml_str."""
    bomb = _fat_scalar_bomb()
    start = time.monotonic()
    with pytest.raises(yaml.YAMLError):
        load_yaml_str(bomb)
    elapsed = time.monotonic() - start
    assert elapsed < _FAST_CEILING_S, (
        f"fat-scalar bomb took {elapsed:.2f}s -- expected a fast fail-closed "
        "raise at parse, not an emitter hang"
    )


def test_fat_scalar_bomb_fails_closed_fast_path(tmp_path: Path) -> None:
    """The path-based load_yaml (apm.yml reader) also fails closed fast."""
    bomb = tmp_path / "apm.yml"
    bomb.write_text(_fat_scalar_bomb(), encoding="utf-8")
    start = time.monotonic()
    with pytest.raises(yaml.YAMLError):
        load_yaml(bomb)
    assert time.monotonic() - start < _FAST_CEILING_S


def test_fat_scalar_bomb_raises_yamlerror_not_a_foreign_exception() -> None:
    """The fail-closed exception is a YAMLError subclass (caught fail-closed)."""
    try:
        load_yaml_str(_fat_scalar_bomb())
    except yaml.YAMLError:
        pass
    except BaseException as exc:  # assertion intent: catch a foreign escape
        pytest.fail(f"bomb raised {type(exc).__name__}, not a yaml.YAMLError")
    else:
        pytest.fail("bomb did not raise -- byte expansion was not bounded")


def test_fat_scalar_bomb_never_reaches_dump_sinks() -> None:
    """A watchdog confirms the bomb dies at parse, before any dump sink runs.

    Parse the bomb on a daemon thread; the moment it (correctly) raises a
    YAMLError we record it. We then assert the dump sinks were never invoked
    on the bomb's data -- they cannot be, because parsing never produced a
    Python object to feed them.
    """
    reached_dump = threading.Event()
    raised_at_parse = threading.Event()

    def _run() -> None:
        try:
            data = load_yaml_str(_fat_scalar_bomb())
        except yaml.YAMLError:
            raised_at_parse.set()
            return
        # Unreachable if the guard holds; if it ever is reached, the dump
        # sink is where the amplification would hang -- flag it BEFORE calling.
        reached_dump.set()
        yaml_to_str(data)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=_FAST_CEILING_S)

    assert not t.is_alive(), "parse/dump did not finish -- emitter amplification hang"
    assert raised_at_parse.is_set(), "bomb did not fail closed at parse"
    assert not reached_dump.is_set(), "bomb reached the amplifying dump sink"


def test_legit_large_scalar_still_parses_and_dumps() -> None:
    """A single large scalar with a few refs stays under budget end-to-end."""
    data = load_yaml_str(_legit_large_scalar())
    assert set(data) == {"name", "version", "big", "a", "b", "c"}
    assert data["a"] == data["big"] == "B" * 4_000
    # The amplification sinks must round-trip it without raising.
    text = yaml_to_str(data)
    assert "name: rt" in text


def test_benign_manifest_round_trips_through_dump_sinks(tmp_path: Path) -> None:
    """A benign manifest still dumps via dump_yaml + yaml_to_str (no regression)."""
    out = tmp_path / "apm.yml"
    dump_yaml(_benign_manifest(), out)
    reloaded = load_yaml(out)
    assert reloaded["lifecycle"]["post-install"][0]["run"] == "echo hi"
    assert "version: 1.0.0" in yaml_to_str(_benign_manifest())
