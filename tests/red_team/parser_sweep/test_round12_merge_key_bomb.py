"""Round-12 parser break r12-parser-1: YAML merge-key expansion bomb.

A linear-size ``apm.yml`` that chains aliased merge keys -- ``<<: [*a, *a]``
once per level -- makes PyYAML's eager ``flatten_mapping`` double the merged
value-list at every level, so the cumulative ``merge.extend`` volume grows
like O(2^N). A sub-kilobyte manifest (n=24, ~900 bytes) drives ``safe_load``
to tens of seconds; a slightly larger one hangs the parser for minutes.

This is a PARSE-time blow-up, so it lands BEFORE any post-parse structural
guard (``_is_fingerprint_safe``) and BEFORE the trust gate -- an untrusted
clone could wedge every ``apm lifecycle`` command AND the unattended
``apm install`` lifecycle fire-path (``parse_apm_yml_lifecycle_with_fingerprint``
-> ``build_runner_from_context``) into a CPU DoS.

The fix reimplements ``flatten_mapping`` in ``_BoundedSafeLoader`` (mirroring
PyYAML 6.x) with a cumulative merged-entry budget plus a recursion-depth cap,
so a hostile manifest raises ``yaml.YAMLError`` within a small fixed budget.
Every ``load_yaml`` consumer already treats ``yaml.YAMLError`` as fail-closed,
so the bomb yields no scripts and ``apm install`` proceeds untouched.

These traps assert: the breadth bomb and a deep merge chain fail closed FAST
(bounded wall-clock, never an unbounded hang); the real ``apm lifecycle``
list/validate commands and the install fire-path do not hang or crash on the
bomb; and legitimate ``<<`` merges (mapping, sequence-of-mappings, moderate
nesting) still resolve to the correct merged values (no semantic regression).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.utils.yaml_io import load_yaml

# Wall-clock ceiling for a fail-closed parse. The pre-fix parse at n=24 is
# ~17s and climbs by 2x per level; the fix returns in ~0.02s. A generous 5s
# ceiling cleanly separates "raised fast" from "exponential hang".
_FAST_CEILING_S = 5.0


def _breadth_bomb(n: int) -> str:
    """Aliased merge bomb: each level merges the previous anchor twice."""
    lines = ["b0: &b0 {x: y}"]
    for i in range(1, n + 1):
        lines += [f"b{i}: &b{i}", f"  <<: [*b{i - 1}, *b{i - 1}]", f"  y{i}: z"]
    lines += ["lifecycle:", f"  post-install: [*b{n}]"]
    return "\n".join(lines) + "\n"


def _deep_chain(n: int) -> str:
    """Single-ref merge chain n levels deep (exercises the depth cap)."""
    lines = ["c0: &c0 {x: y}"]
    for i in range(1, n):
        lines += [f"c{i}: &c{i}", f"  <<: *c{i - 1}", f"  k{i}: {i}"]
    lines += [f"top: [*c{n - 1}]"]
    return "\n".join(lines) + "\n"


def _uncaught(result):
    """Return the non-SystemExit exception a CliRunner surfaced, if any."""
    exc = result.exception
    if exc is None or isinstance(exc, SystemExit):
        return None
    return exc


@pytest.mark.parametrize("n", [24, 40, 64])
def test_breadth_merge_bomb_fails_closed_fast(tmp_path: Path, n: int) -> None:
    """The aliased <<: [*a, *a] bomb raises yaml.YAMLError within the budget."""
    bomb = tmp_path / "apm.yml"
    bomb.write_text(_breadth_bomb(n), encoding="utf-8")

    start = time.monotonic()
    with pytest.raises(yaml.YAMLError):
        load_yaml(bomb)
    elapsed = time.monotonic() - start

    assert elapsed < _FAST_CEILING_S, (
        f"merge bomb n={n} took {elapsed:.2f}s -- expected a fast fail-closed "
        "raise, not an exponential hang"
    )


def test_deep_merge_chain_fails_closed_fast(tmp_path: Path) -> None:
    """A merge chain deeper than the depth cap raises yaml.YAMLError, fast."""
    bomb = tmp_path / "apm.yml"
    bomb.write_text(_deep_chain(600), encoding="utf-8")

    start = time.monotonic()
    with pytest.raises(yaml.YAMLError):
        load_yaml(bomb)
    elapsed = time.monotonic() - start

    assert elapsed < _FAST_CEILING_S


def test_bomb_raises_yamlerror_not_a_foreign_exception(tmp_path: Path) -> None:
    """The fail-closed exception is a YAMLError subclass (caught fail-closed).

    Every load_yaml consumer catches ``yaml.YAMLError`` (or bare Exception) to
    fail closed; a RecursionError / MemoryError escaping that net would crash
    the caller instead. Pin the exception family.
    """
    bomb = tmp_path / "apm.yml"
    bomb.write_text(_breadth_bomb(48), encoding="utf-8")
    try:
        load_yaml(bomb)
    except yaml.YAMLError:
        pass
    except BaseException as exc:  # assertion intent: catch a foreign escape
        pytest.fail(f"bomb raised {type(exc).__name__}, not a yaml.YAMLError")
    else:
        pytest.fail("bomb did not raise -- expansion was not bounded")


def test_lifecycle_discovery_does_not_hang_or_crash_on_bomb(tmp_path) -> None:
    """`discover_scripts` (the `apm lifecycle` list path) fails closed, no hang.

    The bare `apm lifecycle` group callback lists scripts via discover_scripts;
    a bomb apm.yml in the project tier must yield no entries within the budget,
    never an exponential hang.
    """
    from apm_cli.core.lifecycle_scripts import discover_scripts

    (tmp_path / "apm.yml").write_text(_breadth_bomb(40), encoding="utf-8")

    start = time.monotonic()
    entries = discover_scripts(project_root=str(tmp_path))
    elapsed = time.monotonic() - start

    assert elapsed < _FAST_CEILING_S, f"discovery hung for {elapsed:.2f}s on a bomb"
    assert entries == []


def test_lifecycle_validate_does_not_hang_or_crash_on_bomb(tmp_path, monkeypatch) -> None:
    """`apm lifecycle validate` against a bomb apm.yml fails closed, no hang."""
    from apm_cli.commands.lifecycle import lifecycle_validate

    (tmp_path / "apm.yml").write_text(_breadth_bomb(40), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    start = time.monotonic()
    result = CliRunner().invoke(lifecycle_validate, [])
    elapsed = time.monotonic() - start

    assert elapsed < _FAST_CEILING_S, f"validate hung for {elapsed:.2f}s on a bomb"
    assert _uncaught(result) is None


def test_install_fire_path_does_not_hang_or_crash_on_bomb(tmp_path) -> None:
    """The unattended install fire-path fails closed on a bomb manifest.

    ``parse_apm_yml_lifecycle_with_fingerprint`` is what ``build_runner_from_context``
    calls on ``apm install``; a bomb apm.yml must yield no entries (fail
    closed) within the budget, never an exponential hang that wedges install.
    """
    from apm_cli.core.lifecycle_scripts import parse_apm_yml_lifecycle_with_fingerprint

    bomb = tmp_path / "apm.yml"
    bomb.write_text(_breadth_bomb(48), encoding="utf-8")

    start = time.monotonic()
    entries, fingerprint = parse_apm_yml_lifecycle_with_fingerprint(bomb, "project")
    elapsed = time.monotonic() - start

    assert elapsed < _FAST_CEILING_S, f"fire-path hung for {elapsed:.2f}s on a bomb"
    assert entries == []
    assert fingerprint is None


def test_trust_fingerprint_does_not_hang_or_crash_on_bomb(tmp_path) -> None:
    """The trust-gate fingerprint read fails closed on a bomb manifest."""
    from apm_cli.core.script_trust import script_file_fingerprint

    bomb = tmp_path / "apm.yml"
    bomb.write_text(_breadth_bomb(48), encoding="utf-8")

    start = time.monotonic()
    result = script_file_fingerprint(bomb)
    elapsed = time.monotonic() - start

    assert elapsed < _FAST_CEILING_S
    assert result is None


def test_legit_mapping_merge_still_resolves(tmp_path: Path) -> None:
    """A normal <<: *anchor mapping merge is unaffected by the budget."""
    manifest = tmp_path / "apm.yml"
    manifest.write_text(
        "base: &base\n  timeout: 30\n  retries: 3\n"
        "prod:\n  <<: *base\n  region: eu\n  timeout: 60\n",
        encoding="utf-8",
    )
    data = load_yaml(manifest)
    assert data["base"] == {"timeout": 30, "retries": 3}
    assert data["prod"] == {"timeout": 60, "retries": 3, "region": "eu"}


def test_legit_sequence_merge_still_resolves(tmp_path: Path) -> None:
    """A <<: [*a, *b] sequence-of-mappings merge resolves correctly."""
    manifest = tmp_path / "apm.yml"
    manifest.write_text(
        "a: &a {x: 1}\nb: &b {y: 2}\nc:\n  <<: [*a, *b]\n  z: 3\n",
        encoding="utf-8",
    )
    assert load_yaml(manifest)["c"] == {"x": 1, "y": 2, "z": 3}


def test_moderate_merge_nesting_still_parses(tmp_path: Path) -> None:
    """A moderate single-ref merge chain (depth 50) parses well under budget."""
    manifest = tmp_path / "apm.yml"
    manifest.write_text(_deep_chain(50), encoding="utf-8")

    start = time.monotonic()
    data = load_yaml(manifest)
    elapsed = time.monotonic() - start

    assert elapsed < _FAST_CEILING_S
    assert isinstance(data, dict)
    # The terminal anchor accumulated all 50 chained keys via merge.
    assert isinstance(data["c49"], dict)
