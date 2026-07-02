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
