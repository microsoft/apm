"""Fail-closed blast-radius PR slice decisions and the nightly map (#4117)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts import blast_radius as br

pytestmark = pytest.mark.component

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
SRC = "src/apm_cli/install/plan.py"
TEST_FILES = (
    "tests/integration/test_install_plan_lifecycle.py",
    "tests/integration/test_other_lifecycle.py",
    "tests/integration/test_unrelated.py",
)
OWNERS = (
    br.OwnerOverlay(
        "install-plan",
        ("src/apm_cli/install/*.py",),
        ("tests/integration/test_install_*",),
    ),
)
MAP = br.CoverageMap(
    generated_at=NOW - timedelta(hours=1),
    commit="abc",
    files={SRC: ("tests/integration/test_other_lifecycle.py::test_plan",)},
)


def _map_document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": 1,
        "generated_at": (NOW - timedelta(hours=1)).isoformat(),
        "commit": "abc",
        "run_id": "1",
        "mark_expr": br.LANE_MARK_EXPR,
        "test_count": 1,
        "files": {SRC: ["tests/integration/test_other_lifecycle.py::test_plan"]},
    }
    document.update(overrides)
    return document


def _decide(changes: str, coverage_map: br.CoverageMap | None = MAP, failure: str | None = None):
    return br.decide(br.parse_name_status(changes), coverage_map, failure, OWNERS, TEST_FILES)


def test_owned_src_change_selects_map_families_and_changed_tests() -> None:
    decision = _decide(f"M\t{SRC}\nA\ttests/integration/test_unrelated.py\n")

    assert decision.mode == br.MODE_SLICE
    assert decision.owners == ("install-plan",)
    assert decision.targets == (
        "tests/integration/test_install_plan_lifecycle.py",
        "tests/integration/test_other_lifecycle.py::test_plan",
        "tests/integration/test_unrelated.py",
    )


@pytest.mark.parametrize(
    ("changes", "coverage_map", "failure", "reason"),
    [
        (f"M\t{SRC}\n", None, br.REASON_MAP_INVALID, br.REASON_MAP_INVALID),
        (f"M\t{SRC}\n", None, br.REASON_MAP_STALE, br.REASON_MAP_STALE),
        (f"M\t{SRC}\nM\ttests/utils/world.py\n", MAP, None, br.REASON_HARNESS),
        ("M\t.github/workflows/ci.yml\n", MAP, None, br.REASON_HARNESS),
        ("M\tsrc/apm_cli/cli.py\n", MAP, None, br.REASON_UNOWNED),
        ("D\tsrc/apm_cli/gone.py\n", MAP, None, br.REASON_UNOWNED),
        (f"R100\tsrc/apm_cli/old_name.py\t{SRC}\n", MAP, None, br.REASON_UNOWNED),
        ("M\ttests/unit/test_x.py\n", MAP, None, br.REASON_EMPTY),
        (f"M\t{SRC}\n", MAP, br.REASON_MAP_INVALID, br.REASON_MAP_INVALID),
    ],
    ids=[
        "missing-map",
        "stale-map",
        "harness-utils",
        "harness-workflow",
        "unowned",
        "unowned-deletion-old-path",
        "unowned-rename-source",
        "empty-slice-without-src-change",
        "map-failure-overrides-map",
    ],
)
def test_every_untrusted_input_runs_the_full_suite(
    changes: str, coverage_map: br.CoverageMap | None, failure: str | None, reason: str
) -> None:
    decision = _decide(changes, coverage_map, failure)

    assert (decision.mode, decision.reason) == (br.MODE_FULL, reason)
    assert decision.targets == ("tests/integration",)


def test_owned_src_change_with_no_covering_test_runs_the_full_suite() -> None:
    owners = (br.OwnerOverlay("bare", ("src/apm_cli/bare.py",), ()),)
    decision = br.decide(
        br.parse_name_status("M\tsrc/apm_cli/bare.py\n"), MAP, None, owners, TEST_FILES
    )

    assert (decision.mode, decision.reason) == (br.MODE_FULL, br.REASON_EMPTY)


def test_map_nodes_of_removed_test_files_do_not_count_as_coverage() -> None:
    stale_map = br.CoverageMap(NOW, "abc", {SRC: ("tests/integration/test_gone.py::test_x",)})
    owners = (br.OwnerOverlay("bare", (SRC,), ()),)
    decision = br.decide(br.parse_name_status(f"M\t{SRC}\n"), stale_map, None, owners, TEST_FILES)

    assert (decision.mode, decision.reason) == (br.MODE_FULL, br.REASON_EMPTY)


def test_changed_integration_test_alone_is_its_own_slice() -> None:
    decision = _decide("M\ttests/integration/test_unrelated.py\n")

    assert decision.mode == br.MODE_SLICE
    assert decision.targets == ("tests/integration/test_unrelated.py",)


def test_only_non_testable_changes_are_a_noop_with_smoke_as_floor() -> None:
    """Pending scope amendment; flip nothing_testable_changed to restore full."""
    decision = _decide("M\tdocs/src/content/docs/index.md\nM\tREADME.md\n")

    assert (decision.mode, decision.targets) == (br.MODE_NOOP, ())


def test_uncollectable_slice_falls_back_to_the_full_suite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []

    def estimate(_root: Path, targets: tuple[str, ...], _expr: str, _args: Path) -> br.Estimate:
        calls.append(tuple(targets))
        if targets != ("tests/integration",):
            raise RuntimeError("collection failed (4): ERROR: not found")
        return br.Estimate(nodes=10, shard_count=1)

    monkeypatch.setattr(br, "_estimate", estimate)
    decision, executed = br._estimate_or_full(tmp_path, _decide(f"M\t{SRC}\n"), tmp_path / "a")

    assert (decision.mode, decision.reason) == (br.MODE_FULL, br.REASON_COLLECTION)
    assert executed.nodes == 10 and calls[-1] == ("tests/integration",)


@pytest.mark.parametrize(
    ("document", "reason"),
    [
        ("{not json", br.REASON_MAP_INVALID),
        (_map_document(schema_version=2), br.REASON_MAP_INVALID),
        (_map_document(files={"docs/x.md": ["t.py::t"]}), br.REASON_MAP_INVALID),
        (_map_document(generated_at="2026-11-20T10:00:00"), br.REASON_MAP_INVALID),
        (
            _map_document(generated_at=(NOW - timedelta(hours=49)).isoformat()),
            br.REASON_MAP_STALE,
        ),
        (
            _map_document(generated_at=(NOW + timedelta(hours=1)).isoformat()),
            br.REASON_MAP_STALE,
        ),
    ],
    ids=["malformed", "schema", "non-src-key", "naive-timestamp", "stale", "future"],
)
def test_untrusted_maps_are_rejected(tmp_path: Path, document: object, reason: str) -> None:
    path = tmp_path / "map.json"
    path.write_text(document if isinstance(document, str) else json.dumps(document), "utf-8")

    assert br.load_map(path, NOW) == (None, reason)
    assert br.load_map(tmp_path / "absent.json", NOW) == (None, br.REASON_MAP_INVALID)


def test_fresh_map_loads(tmp_path: Path) -> None:
    path = tmp_path / "map.json"
    path.write_text(json.dumps(_map_document()), "utf-8")

    loaded, failure = br.load_map(path, NOW)

    assert failure is None
    assert loaded is not None and loaded.files == MAP.files


def test_build_map_attributes_lines_to_test_contexts(tmp_path: Path) -> None:
    coverage = pytest.importorskip("coverage")
    data = coverage.CoverageData(basename=str(tmp_path / ".coverage"))
    source = tmp_path / "src/apm_cli/mod.py"
    source.parent.mkdir(parents=True)
    source.write_text("x = 1\n", "utf-8")
    for context in (
        "tests/integration/test_a.py::test_one|run",
        "",
        "tests/unit/test_b.py::t|setup",
    ):
        data.set_context(context)
        data.add_lines({str(source): [1]})
    data.add_lines({str(tmp_path / "elsewhere.py"): [1]})
    data.write()

    files = br.build_map(tmp_path / ".coverage", tmp_path)

    assert files == {
        "src/apm_cli/mod.py": [
            "tests/integration/test_a.py::test_one",
            "tests/unit/test_b.py::t",
        ]
    }
