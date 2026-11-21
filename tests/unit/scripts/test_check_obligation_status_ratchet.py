"""Fixture contracts for the obligation status ratchet (#4586)."""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-native-lifecycle-evidence"),
]

RATCHET_TEST_SCOPE = "fixture"

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check_obligation_status_ratchet.py"
WITNESS = "tests/lifecycle/test_x.py::test_y"
T1 = "T1|OP=install|PRIM=skill"
G1 = "G|install|local-deny|hook"


def _module():
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        spec = importlib.util.spec_from_file_location("check_obligation_status_ratchet", SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SCRIPT.parent))


ratchet = _module()


def _files(*, t1: str | None = "required", g1: str = "required") -> dict[str, dict]:
    """One required id in each obligation file, unless overridden."""
    generated: dict[str, dict] = {"required": {}, "known_failing": {}}
    if g1 == "required":
        generated["required"][G1] = WITNESS
    elif g1 == "known":
        generated["known_failing"][G1] = "#1"
    rows = {T1: {"status": t1}} if t1 else {}
    return {
        "lifecycle": {"obligations": rows, "removed": {}, "mapped": {}},
        "generated": generated,
        "security": {"excluded": {}},
        "removals": {"removals": {}},
    }


def test_an_unchanged_head_is_clean() -> None:
    assert ratchet.demoted_between(_files(), _files()) == []


@pytest.mark.parametrize(
    ("head", "lost"),
    [
        (_files(t1="baseline"), [T1]),
        (_files(g1="baseline"), [G1]),
        (_files(t1=None), [T1]),
    ],
    ids=["lifecycle-baseline", "generated-baseline", "lifecycle-absent"],
)
def test_a_demoted_or_absent_required_id_fails(head: dict, lost: list[str]) -> None:
    assert ratchet.demoted_between(_files(), head) == lost


def test_a_known_failing_id_is_still_tracked() -> None:
    assert ratchet.demoted_between(_files(), _files(t1="known-failing", g1="known")) == []


@pytest.mark.parametrize(
    ("name", "key", "value"),
    [
        ("lifecycle", "removed", ["U8"]),
        ("lifecycle", "mapped", {"maps_to": "T1|OP=install|PRIM=agent", "rules": ["E3"]}),
        ("removals", "removals", "Q&L ruling, residual #9999"),
    ],
    ids=["removed-rule", "mapped-rule", "named-removal"],
)
def test_a_rule_the_change_names_allows_the_demotion(name: str, key: str, value: object) -> None:
    head = _files(t1="baseline")
    head[name][key][T1] = value
    assert ratchet.demoted_between(_files(), head) == []


def test_a_rule_already_at_the_base_does_not_excuse_a_new_demotion() -> None:
    base = _files()
    base["removals"]["removals"][T1] = "old ruling"
    head = copy.deepcopy(base)
    head["lifecycle"]["obligations"][T1]["status"] = "baseline"
    assert ratchet.demoted_between(base, head) == [T1]


def test_main_fails_on_a_demotion_and_names_the_way_out(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    states = {"base": _files(), None: _files(g1="baseline")}
    monkeypatch.setattr(ratchet, "_load", lambda root, ref: states["base" if ref else None])
    assert ratchet.main(["--base-ref", "abc"]) == 1
    out = capsys.readouterr().out
    assert G1 in out and "obligation_removals.json" in out
    states[None] = _files()
    assert ratchet.main(["--base-ref", "abc"]) == 0


def test_an_unreadable_base_ref_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def unknown(*args: object, **kwargs: object) -> None:
        raise ratchet.BaselineError("unknown git ref for lifecycle_obligations.json baseline")

    monkeypatch.setattr(ratchet, "load_baseline_at_ref", unknown)
    assert ratchet.main(["--base-ref", "0" * 40]) == 2
