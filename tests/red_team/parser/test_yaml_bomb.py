"""RED-TEAM: YAML alias / billion-laughs bomb against the lifecycle parser.

Threat model: an untrusted cloned repo (or a corrupt ``~/.apm/apm.yml``)
ships an ``apm.yml`` whose ``lifecycle:`` block uses deeply nested YAML
anchors/aliases (classic billion-laughs). If the sanctioned loader
(:func:`apm_cli.utils.yaml_io.load_yaml`) expanded aliases into copies,
parsing a few hundred bytes would balloon into 9**12 nodes = a memory /
CPU DoS.

Result on head: SECURE, by an EXPLICIT loader budget. The original
analysis here assumed ``yaml.safe_load`` was safe because it resolves
aliases to *shared references* (a DAG), making parse cost linear in the
document text. Round-13 (r13-parser-1) disproved that comfort: the
shared-ref DAG is a LATENT bomb that detonates in any consumer that
materializes it (``str()`` in ``_safe_token``, ``deepcopy``, re-serialize)
-- ``apm lifecycle validate`` / ``test`` hung for minutes on a sub-kilobyte
manifest. So ``load_yaml`` (``_BoundedSafeLoader``) now walks the composed
node graph and FAILS CLOSED with ``yaml.YAMLError`` the instant the
per-occurrence expansion weight crosses a fixed budget -- the bomb is
rejected at parse, before any consumer can re-expand it. Every
``load_yaml`` consumer already treats ``yaml.YAMLError`` as fail-closed
(empty / None / exit 1), so the lifecycle parser still yields zero entries.

Every test here is wall-clock guarded: a vulnerable loader that actually
expanded would blow the time bound and fail fast in a daemon thread,
never wedging CI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .conftest import FIXTURES, run_guarded

BOMB_UNKNOWN = FIXTURES / "apm_yml" / "bomb_unknown_events.yml"
BOMB_UNDER_EVENT = FIXTURES / "apm_yml" / "bomb_under_event.yml"


def test_bomb_fixtures_present():
    assert BOMB_UNKNOWN.is_file()
    assert BOMB_UNDER_EVENT.is_file()
    # The fixtures are tiny on disk -- the danger is purely in expansion.
    assert BOMB_UNKNOWN.stat().st_size < 4096
    assert BOMB_UNDER_EVENT.stat().st_size < 4096


def test_load_yaml_fails_closed_on_alias_bomb():
    """A depth-12 9-way alias bomb is REJECTED at parse (fail closed).

    The fixture's notional expansion is 9**12 (~2.8e11 leaves). The
    round-13 expansion-weight budget rejects it the instant the weight
    crosses the cap -- ``load_yaml`` raises ``yaml.YAMLError`` fast rather
    than returning a shared-ref DAG that a downstream ``str()`` could
    detonate. (The pre-r13 contract -- parse succeeds, aliases stay shared
    -- was the disproven non-goal.)
    """
    import yaml

    from apm_cli.utils.yaml_io import load_yaml

    finished, result, exc = run_guarded(lambda: load_yaml(BOMB_UNDER_EVENT), timeout=8.0)
    assert finished, "load_yaml did not finish in time -- alias-expansion DoS"
    assert result is None
    assert isinstance(exc, yaml.YAMLError), (
        f"alias bomb must fail closed with yaml.YAMLError, got {exc!r}"
    )


def test_parser_ignores_unknown_event_bomb_quickly():
    """Bomb under non-event keys (lvl0..lvl12) -> parser yields zero entries."""
    from apm_cli.core.lifecycle_scripts import parse_apm_yml_lifecycle

    finished, result, exc = run_guarded(
        lambda: parse_apm_yml_lifecycle(BOMB_UNKNOWN, "project"), timeout=8.0
    )
    assert finished, "parse_apm_yml_lifecycle hung on the bomb"
    assert exc is None, f"parser raised on bomb: {exc!r}"
    assert result == []


def test_parser_shallow_iterates_bomb_under_real_event():
    """Bomb whose list IS the post-install value -> shallow iteration only."""
    from apm_cli.core.lifecycle_scripts import parse_apm_yml_lifecycle

    finished, result, exc = run_guarded(
        lambda: parse_apm_yml_lifecycle(BOMB_UNDER_EVENT, "project"), timeout=8.0
    )
    assert finished, "parser hung on event-anchored bomb"
    assert exc is None, f"parser raised: {exc!r}"
    # load_yaml now fails closed on the bomb (expansion-weight budget), so the
    # parser catches the YAMLError internally and yields zero entries -- the
    # bomb's value list is never even reached.
    assert result == []


@pytest.mark.parametrize("levels", [8, 14, 20])
def test_deep_alias_bomb_fails_closed_fast(tmp_path: Path, levels: int):
    """A 9-way alias chain past the weight budget fails closed, never hangs.

    9**8 (~43M) already exceeds the 5M expansion-weight budget, so every
    one of these depths is rejected at parse with ``yaml.YAMLError`` -- a
    bounded, fast fail-closed, never the exponential materialization a
    copying (or unbounded) loader would suffer.
    """
    import yaml

    from apm_cli.utils.yaml_io import load_yaml

    lines = ['  l0: &b0 "x"']
    prev = "b0"
    for i in range(1, levels + 1):
        refs = ", ".join([f"*{prev}"] * 9)
        lines.append(f"  l{i}: &b{i} [{refs}]")
        prev = f"b{i}"
    doc = tmp_path / "apm.yml"
    doc.write_text("lifecycle:\n" + "\n".join(lines) + "\n", encoding="utf-8")

    finished, _result, exc = run_guarded(lambda: load_yaml(doc), timeout=6.0)
    assert finished, f"depth={levels}: load did not finish -- expansion DoS"
    assert isinstance(exc, yaml.YAMLError), (
        f"depth={levels}: bomb must fail closed with yaml.YAMLError, got {exc!r}"
    )


@pytest.mark.parametrize("levels", [1, 2, 3])
def test_shallow_alias_doc_still_parses_under_budget(tmp_path: Path, levels: int):
    """A shallow 9-way alias doc (9**3 = 729 << budget) parses, no false positive."""
    from apm_cli.utils.yaml_io import load_yaml

    lines = ['  l0: &b0 "x"']
    prev = "b0"
    for i in range(1, levels + 1):
        refs = ", ".join([f"*{prev}"] * 9)
        lines.append(f"  l{i}: &b{i} [{refs}]")
        prev = f"b{i}"
    doc = tmp_path / "apm.yml"
    doc.write_text("lifecycle:\n" + "\n".join(lines) + "\n", encoding="utf-8")

    finished, result, exc = run_guarded(lambda: load_yaml(doc), timeout=6.0)
    assert finished, f"depth={levels}: load did not finish"
    assert exc is None, f"depth={levels}: shallow doc wrongly rejected: {exc!r}"
    assert isinstance(result, dict)


def test_large_anchor_free_lockfile_loads_successfully(tmp_path: Path):
    """Regression for issue #2389: large anchor-free lockfile must not be rejected.

    APM-generated lockfiles can accumulate a YAML node-weight sum that exceeds
    the 5M alias-expansion budget purely through literal size, with zero anchors
    or aliases.  Before the two-budget fix ``_guard_expansion`` raised a false-
    positive "billion-laughs expansion bomb" error.  After the fix, anchor-free
    documents bypass the tight expansion cap entirely.

    The generated document contains more than 5,000,000 scalar bytes across
    thousands of mapping entries (no anchors, no aliases) -- above the 5M
    weight threshold -- and must load without error.
    """
    from apm_cli.utils.yaml_io import load_yaml

    # Build a document whose accumulated leaf byte cost exceeds 5_000_000
    # without spending CI time on hundreds of thousands of YAML nodes.
    entry_count = 6_000
    value = "abcdef1234567890" * 64
    lines = ["packages:\n"]
    for i in range(entry_count):
        lines.append(f"  pkg{i:06d}: {value}\n")
    doc = tmp_path / "apm.lock.yaml"
    doc.write_text("".join(lines), encoding="utf-8")

    finished, result, exc = run_guarded(lambda: load_yaml(doc), timeout=30.0)
    assert finished, "load_yaml timed out on large anchor-free lockfile"
    assert exc is None, f"large anchor-free lockfile wrongly rejected (false positive): {exc!r}"
    assert isinstance(result, dict)
    assert len(result["packages"]) == entry_count


def test_merge_key_bomb_still_rejected(tmp_path: Path):
    """Merge-key bomb is rejected by the merge-entry budget regardless of alias check.

    A merge-key chain (``<<: [*a, *a]`` at each level) uses aliases AND the
    ``<<`` merge key, so both the alias check and the merge-entry guard engage.
    The level count is chosen so that the per-node expansion weight stays under
    the 5M alias-expansion budget (levels=17 yields ~2.6M combined weight) while
    the doubling merge entries (2**17 = 131,072 > 100,000) trigger the
    merge-entry budget first.  This verifies the two-budget refactor did not
    break the orthogonal ``flatten_mapping`` / merge-entry guard path.
    """
    import yaml

    from apm_cli.utils.yaml_io import load_yaml

    # 17 levels of doubling merge: 2**17 = 131_072 > _MAX_MERGE_ENTRIES (100_000).
    # Combined expansion weight is ~2.6M -- below the 5M alias-expansion cap --
    # so the merge-entry budget fires first and the alias-expansion guard is a
    # no-op for this case.
    levels = 17
    lines = ["a0: &a0\n  x: 1\n"]
    prev = "a0"
    for i in range(1, levels + 1):
        lines.append(f"a{i}: &a{i}\n  <<: [*{prev}, *{prev}]\n  y{i}: {i}\n")
        prev = f"a{i}"
    doc = tmp_path / "merge_bomb.yml"
    doc.write_text("".join(lines), encoding="utf-8")

    finished, _result, exc = run_guarded(lambda: load_yaml(doc), timeout=8.0)
    assert finished, "load_yaml hung on merge-key bomb"
    assert isinstance(exc, yaml.YAMLError), (
        f"merge-key bomb must be rejected with yaml.YAMLError, got {exc!r}"
    )
    assert "merge-key expansion exceeded the safe budget" in str(exc), (
        f"merge-entry guard must fire first, not alias-expansion guard; got {exc!r}"
    )
