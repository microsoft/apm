"""Round-13 parser break r13-parser-1: pure-alias expansion bomb.

The round-12 fix bounded PyYAML's merge-key (``<<``) flatten volume, but a
``<<``-free **pure-alias** billion-laughs bomb slipped past it. PyYAML's
``SafeLoader`` shares ONE node object across every ``*alias``, so

    l0: &l0 [z, z]
    l1: &l1 [*l0, *l0]
    ...
    lN: &lN [*l(N-1), *l(N-1)]

composes in O(N) objects at parse time and is NOT a merge map, so the
round-12 merge-entry budget never fires. The exponential cost only
materializes when a consumer walks the shared-ref DAG -- e.g.
``_safe_token(script_type)`` (``apm lifecycle validate`` / ``test``) does
``str(value)`` on the aliased ``type:`` node, re-expanding O(2^N). A
sub-kilobyte manifest (n~30) then wedges the command (the GIL-monopolizing
``str()`` starves even a watchdog thread) -- a CPU DoS on every
``apm lifecycle`` command reading an untrusted clone's manifest.

This lands BEFORE the trust gate (validate/test never run ``_is_fingerprint_safe``),
so the round-12 non-goal "pure-alias bombs are caught post-parse by
``_is_fingerprint_safe``" covered only the TRUST path, not these non-trust
consumers -- making the hang genuine.

The primary fix adds a loader-level expansion-weight guard
(``_guard_expansion`` in ``_BoundedSafeLoader.construct_document``): a
memoized per-node DAG walk that sums child weights PER OCCURRENCE (so a
shared node inflates the running total even though it is walked once) and
fails closed with ``yaml.YAMLError`` the instant the total crosses a fixed
budget. A defense-in-depth layer hardens ``_safe_token`` to never ``str()``
a container (returns ``<list>`` / ``<dict>`` etc.) and widens its except to
``(ValueError, RecursionError, MemoryError, OverflowError)``.

These traps assert: the pure-alias bomb (and a self-referential cycle) fail
closed FAST at ``load_yaml``; the real ``apm lifecycle`` validate/list and
the install fire-path do not hang or crash; ``_safe_token`` is bounded on a
shared-ref container; and legitimate benign DAG anchors (a list reused many
times, moderate reuse) still parse to the correct values (no regression).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.utils.yaml_io import load_yaml

# The pre-fix path hangs the interpreter indefinitely (GIL-monopolizing
# ``str()`` of the shared-ref DAG); the fix raises in ~0.003s. A generous 5s
# ceiling cleanly separates "raised fast" from "exponential hang".
_FAST_CEILING_S = 5.0


def _alias_bomb(n: int) -> str:
    """Pure-alias (``<<``-free) billion-laughs bomb, aliased into ``type:``."""
    lines = ["name: rt", "version: 1.0.0", "l0: &l0 [z, z]"]
    for i in range(1, n + 1):
        lines.append(f"l{i}: &l{i} [*l{i - 1}, *l{i - 1}]")
    lines += ["lifecycle:", "  pre-install:", f"    - type: *l{n}", "      run: echo hi"]
    return "\n".join(lines) + "\n"


def _self_cycle() -> str:
    """A self-referential anchor -> a cyclic node graph (must fail closed)."""
    return "name: rt\nversion: 1.0.0\na: &a [*a]\nlifecycle:\n  pre-install: [*a]\n"


def _uncaught(result):
    """Return the non-SystemExit exception a CliRunner surfaced, if any."""
    exc = result.exception
    if exc is None or isinstance(exc, SystemExit):
        return None
    return exc


@pytest.mark.parametrize("n", [24, 30, 48])
def test_alias_bomb_fails_closed_fast(tmp_path: Path, n: int) -> None:
    """The pure-alias ``[*a, *a]`` bomb raises yaml.YAMLError within budget."""
    bomb = tmp_path / "apm.yml"
    bomb.write_text(_alias_bomb(n), encoding="utf-8")

    start = time.monotonic()
    with pytest.raises(yaml.YAMLError):
        load_yaml(bomb)
    elapsed = time.monotonic() - start

    assert elapsed < _FAST_CEILING_S, (
        f"alias bomb n={n} took {elapsed:.2f}s -- expected a fast fail-closed "
        "raise, not an exponential hang"
    )


def test_self_referential_cycle_fails_closed_fast(tmp_path: Path) -> None:
    """A cyclic anchor graph fails closed (no infinite recursion / hang)."""
    bomb = tmp_path / "apm.yml"
    bomb.write_text(_self_cycle(), encoding="utf-8")

    start = time.monotonic()
    with pytest.raises(yaml.YAMLError):
        load_yaml(bomb)
    elapsed = time.monotonic() - start

    assert elapsed < _FAST_CEILING_S


def test_alias_bomb_raises_yamlerror_not_a_foreign_exception(tmp_path: Path) -> None:
    """The fail-closed exception is a YAMLError subclass (caught fail-closed)."""
    bomb = tmp_path / "apm.yml"
    bomb.write_text(_alias_bomb(48), encoding="utf-8")
    try:
        load_yaml(bomb)
    except yaml.YAMLError:
        pass
    except BaseException as exc:  # assertion intent: catch a foreign escape
        pytest.fail(f"bomb raised {type(exc).__name__}, not a yaml.YAMLError")
    else:
        pytest.fail("bomb did not raise -- expansion was not bounded")


def test_lifecycle_validate_does_not_hang_or_crash_on_alias_bomb(tmp_path, monkeypatch) -> None:
    """`apm lifecycle validate` against the alias bomb fails closed, no hang."""
    from apm_cli.commands.lifecycle import lifecycle_validate

    (tmp_path / "apm.yml").write_text(_alias_bomb(40), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    start = time.monotonic()
    result = CliRunner().invoke(lifecycle_validate, [])
    elapsed = time.monotonic() - start

    assert elapsed < _FAST_CEILING_S, f"validate hung for {elapsed:.2f}s on a bomb"
    assert _uncaught(result) is None


def test_lifecycle_discovery_does_not_hang_or_crash_on_alias_bomb(tmp_path) -> None:
    """`discover_scripts` (the `apm lifecycle` list path) fails closed, no hang."""
    from apm_cli.core.lifecycle_scripts import discover_scripts

    (tmp_path / "apm.yml").write_text(_alias_bomb(40), encoding="utf-8")

    start = time.monotonic()
    entries = discover_scripts(project_root=str(tmp_path))
    elapsed = time.monotonic() - start

    assert elapsed < _FAST_CEILING_S, f"discovery hung for {elapsed:.2f}s on a bomb"
    assert entries == []


def test_install_fire_path_does_not_hang_or_crash_on_alias_bomb(tmp_path) -> None:
    """The unattended install fire-path fails closed on the alias bomb."""
    from apm_cli.core.lifecycle_scripts import parse_apm_yml_lifecycle_with_fingerprint

    bomb = tmp_path / "apm.yml"
    bomb.write_text(_alias_bomb(48), encoding="utf-8")

    start = time.monotonic()
    entries, fingerprint = parse_apm_yml_lifecycle_with_fingerprint(bomb, "project")
    elapsed = time.monotonic() - start

    assert elapsed < _FAST_CEILING_S, f"fire-path hung for {elapsed:.2f}s on a bomb"
    assert entries == []
    assert fingerprint is None


def test_safe_token_bounded_on_shared_ref_container() -> None:
    """`_safe_token` never ``str()``s a container -> bounded on a shared DAG.

    Even without the loader guard (defense in depth), a deeply shared-ref list
    -- 2^60 logical leaves in O(60) objects -- must degrade to a type name
    instantly, never trigger the exponential ``str()`` materialization.
    """
    from apm_cli.commands.lifecycle import _safe_token

    shared = [0, 0]
    for _ in range(60):
        shared = [shared, shared]

    start = time.monotonic()
    token = _safe_token(shared)
    elapsed = time.monotonic() - start

    assert token == "<list>"
    assert elapsed < _FAST_CEILING_S


def test_legit_reused_anchor_dag_still_parses(tmp_path: Path) -> None:
    """A benign anchor reused many times (low weight) parses correctly.

    The expansion guard must NOT false-positive on ordinary anchor reuse:
    a small shared list referenced from dozens of keys stays far below the
    weight budget and resolves to the same value at every site.
    """
    lines = ["name: rt", "version: 1.0.0", "common: &c [a, b, c]"]
    for i in range(40):
        lines.append(f"k{i}: *c")
    manifest = tmp_path / "apm.yml"
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")

    data = load_yaml(manifest)
    assert data["common"] == ["a", "b", "c"]
    assert data["k0"] == ["a", "b", "c"]
    assert data["k39"] == ["a", "b", "c"]


def test_legit_moderate_alias_nesting_still_parses(tmp_path: Path) -> None:
    """A moderate (non-doubling) alias chain stays under budget and parses."""
    lines = ["name: rt", "version: 1.0.0", "n0: &n0 [leaf]"]
    for i in range(1, 12):
        lines.append(f"n{i}: &n{i} [*n{i - 1}, extra{i}]")
    manifest = tmp_path / "apm.yml"
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")

    start = time.monotonic()
    data = load_yaml(manifest)
    elapsed = time.monotonic() - start

    assert elapsed < _FAST_CEILING_S
    assert isinstance(data["n11"], list)
