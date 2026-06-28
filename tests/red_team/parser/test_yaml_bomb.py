"""RED-TEAM: YAML alias / billion-laughs bomb against the lifecycle parser.

Threat model: an untrusted cloned repo (or a corrupt ``~/.apm/apm.yml``)
ships an ``apm.yml`` whose ``lifecycle:`` block uses deeply nested YAML
anchors/aliases (classic billion-laughs). If the sanctioned loader
(:func:`apm_cli.utils.yaml_io.load_yaml`) expanded aliases into copies,
parsing a few hundred bytes would balloon into 9**12 nodes = a memory /
CPU DoS.

Result on head: SECURE. ``load_yaml`` uses ``yaml.safe_load``, which
resolves aliases to *shared references* (a DAG), so parse cost is linear
in the document text, not in the notional expansion. The lifecycle parser
additionally ignores unknown top-level event keys and only shallow-iterates
a real event's script list, so the bomb never forces a deep walk.

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


def test_safe_load_keeps_shared_refs_not_copies():
    """Aliases must resolve to the SAME object, proving no expansion."""
    from apm_cli.utils.yaml_io import load_yaml

    finished, result, exc = run_guarded(lambda: load_yaml(BOMB_UNDER_EVENT), timeout=8.0)
    assert finished, "load_yaml did not finish in time -- alias-expansion DoS"
    assert exc is None, f"load_yaml raised: {exc!r}"

    post_install = result["lifecycle"]["post-install"]
    # 9 siblings, every one the identical shared child list (a DAG node).
    assert len(post_install) == 9
    first = post_install[0]
    assert all(sibling is first for sibling in post_install), (
        "alias targets were copied, not shared -- billion-laughs expansion"
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
    # The 9 top-level elements are lists (shared sub-bombs), not dicts, so
    # _build_entry drops them all -- bounded, never a deep traversal.
    assert result == []


@pytest.mark.parametrize("levels", [8, 14, 20])
def test_parse_time_is_linear_in_depth(tmp_path: Path, levels: int):
    """Parse cost grows ~linearly with depth, never exponentially."""
    from apm_cli.utils.yaml_io import load_yaml

    lines = ['  l0: &b0 "x"']
    prev = "b0"
    for i in range(1, levels + 1):
        refs = ", ".join([f"*{prev}"] * 9)
        lines.append(f"  l{i}: &b{i} [{refs}]")
        prev = f"b{i}"
    doc = tmp_path / "apm.yml"
    doc.write_text("lifecycle:\n" + "\n".join(lines) + "\n", encoding="utf-8")

    # Generous per-depth bound; a copying loader would explode long before.
    finished, _result, exc = run_guarded(lambda: load_yaml(doc), timeout=6.0)
    assert finished, f"depth={levels}: load did not finish -- expansion DoS"
    assert exc is None, f"depth={levels}: loader raised {exc!r}"
