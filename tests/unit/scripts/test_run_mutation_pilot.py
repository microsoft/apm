"""Tests for the owner-scoped mutation kill-rate floor runner."""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
import tomllib
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "run_mutation_pilot.py"
FLOORS_PATH = REPO_ROOT / "tests" / "mutation" / "floors.json"
OWNER_REGISTRY = REPO_ROOT / ".apm" / "architecture" / "owners"


def _load_module() -> ModuleType:
    scripts_dir = str(SCRIPT_PATH.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location("run_mutation_pilot", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def pilot() -> ModuleType:
    return _load_module()


def _checked_in_payload() -> dict:
    return json.loads(FLOORS_PATH.read_text(encoding="utf-8"))


def _entry(*, floor: float = 1.0, equivalents: list | None = None) -> dict:
    return {
        "architecture_owner": "install-outcome",
        "equivalents": equivalents or [],
        "floor": floor,
        "measured": {},
        "targets": [{"functions": ["fn", "Klass.method"], "source": "src/apm_cli/x/mod.py"}],
        "test_seams": ["tests/unit/test_mod.py"],
    }


def _payload(**owners: dict) -> dict:
    return {
        "kill_rate_definition": "(killed + timeout) / (total - equivalent)",
        "owners": owners or {"demo": _entry()},
        "schema_version": 2,
        "tool": {"name": "mutmut", "version": "3.6.0"},
    }


def _owner(pilot: ModuleType, **kwargs: object) -> object:
    return pilot.parse_floors(_payload(demo=_entry(**kwargs)))[0]


def _equivalent(mutant: str, kind: str = "semantic-equivalent", reason: str = "") -> dict:
    return {
        "class": kind,
        "mutant": mutant,
        "reason": reason or "Both branches return the same normalized value here.",
    }


# --- schema and checked-in data ------------------------------------------------


def test_checked_in_floors_parse_and_never_exceed_measured_rate(pilot: ModuleType) -> None:
    payload = _checked_in_payload()
    owners = pilot.parse_floors(payload)

    assert len(owners) >= 20
    for owner in owners:
        measured = payload["owners"][owner.key]["measured"]
        assert owner.floor <= measured["kill_rate"], owner.key
        assert measured["total"] > 0, owner.key


def test_checked_in_owners_map_to_registered_architecture_owners(pilot: ModuleType) -> None:
    index = json.loads((OWNER_REGISTRY / "index.json").read_text(encoding="utf-8"))
    registered = {
        owner["id"]
        for shard in index["shards"]
        for owner in json.loads((OWNER_REGISTRY / shard).read_text(encoding="utf-8"))["owners"]
    }
    owners = pilot.parse_floors(_checked_in_payload())

    unmapped = {owner.key for owner in owners if owner.architecture_owner is None}
    assert unmapped == {"link-projection"}
    assert {o.architecture_owner for o in owners if o.architecture_owner} <= registered


def _function_nodes(source: Path) -> dict[str, ast.AST]:
    tree = ast.parse(source.read_text(encoding="utf-8"))
    nodes: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            nodes[node.name] = node
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    nodes[f"{node.name}.{child.name}"] = node
    return nodes


def test_checked_in_targets_exist_and_are_mutable_by_mutmut(pilot: ModuleType) -> None:
    for owner in pilot.parse_floors(_checked_in_payload()):
        for seam in owner.test_seams:
            assert (REPO_ROOT / seam.partition("::")[0]).is_file(), seam
        for target in owner.targets:
            nodes = _function_nodes(REPO_ROOT / target.source)
            for function in target.functions:
                assert function in nodes, f"{owner.key}: {target.source}:{function}"
                # mutmut 3.6.0 skips every method of a decorated class.
                container = nodes[function]
                if isinstance(container, ast.ClassDef):
                    assert not container.decorator_list, f"{owner.key}: {function}"


def test_mutmut_config_matches_floors(pilot: ModuleType) -> None:
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    mutmut = config["tool"]["mutmut"]
    owners = pilot.parse_floors(_checked_in_payload())

    seams: list[str] = []
    for owner in owners:
        seams.extend(seam for seam in owner.test_seams if seam not in seams)
    assert mutmut["only_mutate"] == sorted(
        {target.source for owner in owners for target in owner.targets}
    )
    assert mutmut["pytest_add_cli_args_test_selection"] == seams


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda p: p.update(schema_version=1), "unsupported schema_version"),
        (lambda p: p.update(tool={"name": "mutmut", "version": "9"}), "tool version"),
        (lambda p: p.update(kill_rate_definition="killed/total"), "kill_rate_definition"),
        (lambda p: p["owners"]["demo"].update(floor=1.5), "floor must be within"),
        (lambda p: p["owners"]["demo"].update(floor=True), "floor must be within"),
        (lambda p: p["owners"]["demo"].update(extra=1), "malformed owner demo"),
        (lambda p: p["owners"]["demo"].update(test_seams=[]), "non-empty string list"),
        (
            lambda p: p["owners"]["demo"]["targets"][0].update(source="lib/x.py"),
            "under src/",
        ),
        (
            lambda p: p["owners"]["demo"].update(
                equivalents=[_equivalent("m.f__mutmut_2"), _equivalent("m.f__mutmut_1")]
            ),
            "sorted, unique",
        ),
        (
            lambda p: p["owners"]["demo"].update(
                equivalents=[_equivalent("m.f__mutmut_1", kind="cosmetic")]
            ),
            "class 'cosmetic'",
        ),
        (
            lambda p: p["owners"]["demo"].update(
                equivalents=[_equivalent("m.f__mutmut_1", reason="same")]
            ),
            "needs a reason",
        ),
    ],
)
def test_parse_floors_rejects_malformed_documents(
    pilot: ModuleType, mutate: object, message: str
) -> None:
    payload = _payload()
    mutate(payload)

    with pytest.raises(pilot.BaselineError, match=message):
        pilot.parse_floors(payload)


def test_user_facing_text_cannot_be_declared_unobservable(pilot: ModuleType) -> None:
    payload = _payload(
        demo=_entry(
            equivalents=[
                _equivalent(
                    "m.f__mutmut_1",
                    kind="unobservable-diagnostic-text",
                    reason="Error message wording shown to the user on failure.",
                )
            ]
        )
    )

    with pytest.raises(pilot.BaselineError, match="'debug:' or 'log-only:'"):
        pilot.parse_floors(payload)


def test_debug_text_may_be_declared_unobservable(pilot: ModuleType) -> None:
    owner = _owner(
        pilot,
        equivalents=[
            _equivalent(
                "m.f__mutmut_1",
                kind="unobservable-diagnostic-text",
                reason="debug: logger.debug text in src/apm_cli/x/mod.py:fn",
            )
        ],
    )

    assert set(owner.equivalents) == {"m.f__mutmut_1"}


# --- patterns and names --------------------------------------------------------


def test_patterns_are_exact_per_function_and_method(pilot: ModuleType) -> None:
    owner = _owner(pilot)
    separator = chr(0x01C1)

    assert owner.patterns == (
        "apm_cli.x.mod.x_fn__mutmut_*",
        f"apm_cli.x.mod.x{separator}Klass{separator}method__mutmut_*",
    )


def test_pattern_does_not_capture_prefix_sharing_function(pilot: ModuleType) -> None:
    import fnmatch

    pattern = _owner(pilot).patterns[0]

    assert fnmatch.fnmatchcase("apm_cli.x.mod.x_fn__mutmut_3", pattern)
    assert not fnmatch.fnmatchcase("apm_cli.x.mod.x_fn_other__mutmut_3", pattern)


def test_canonical_mutant_name_removes_internal_mangling(pilot: ModuleType) -> None:
    separator = chr(0x01C1)

    assert (
        pilot._canonical_mutant_name(f"a.b.x{separator}Klass{separator}_m__mutmut_4")
        == "a.b.Klass._m__mutmut_4"
    )
    assert pilot._canonical_mutant_name("a.b.x_fn__mutmut_2") == "a.b.fn__mutmut_2"
    with pytest.raises(pilot.PilotError, match="invalid mutmut name"):
        pilot._canonical_mutant_name("missing-module")


def test_signal_outcomes_are_classified_accurately(pilot: ModuleType) -> None:
    assert pilot.STATUS_BY_EXIT_CODE[-9] == "terminated"
    assert pilot.STATUS_BY_EXIT_CODE[-11] == "segfault"
    assert {"segfault", "terminated"} <= pilot.FATAL_STATUSES
    assert pilot.DETECTED_STATUSES == {"killed", "timeout"}
    assert pilot.UNDETECTED_STATUSES == {"survived", "no_tests"}


# --- kill rate and floors ------------------------------------------------------


def test_kill_rate_excludes_only_surviving_equivalents(pilot: ModuleType) -> None:
    owner = _owner(
        pilot,
        floor=0.75,
        equivalents=[_equivalent("m.fn__mutmut_4"), _equivalent("m.fn__mutmut_9")],
    )
    outcomes = {
        "killed": ["m.fn__mutmut_1", "m.fn__mutmut_2", "m.fn__mutmut_9"],
        "timeout": ["m.fn__mutmut_3"],
        "survived": ["m.fn__mutmut_4", "m.fn__mutmut_5"],
        "no_tests": ["m.fn__mutmut_6"],
    }

    report = pilot.owner_report(owner, outcomes)

    assert report["measured"] == {
        "equivalent": 1,
        "kill_rate": round(4 / 6, 4),
        "killed": 3,
        "no_tests": 1,
        "survived": 2,
        "timeout": 1,
        "total": 7,
    }
    assert report["status"] == "regression"
    assert report["unlisted_survivors"] == ["m.fn__mutmut_5", "m.fn__mutmut_6"]
    assert report["killed_equivalents"] == ["m.fn__mutmut_9"]


def test_rate_at_floor_is_accepted_and_stale_equivalents_reported(pilot: ModuleType) -> None:
    owner = _owner(pilot, floor=0.5, equivalents=[_equivalent("m.fn__mutmut_7")])

    report = pilot.owner_report(owner, {"killed": ["m.a"], "survived": ["m.b"]})

    assert report["measured"]["kill_rate"] == 0.5
    assert report["status"] == "accepted"
    assert report["stale_equivalents"] == ["m.fn__mutmut_7"]


@pytest.mark.parametrize("status", ["skipped", "suspicious", "type_checked", "not_checked"])
def test_incomplete_outcome_fails_even_above_floor(pilot: ModuleType, status: str) -> None:
    owner = _owner(pilot, floor=0.0)

    report = pilot.owner_report(owner, {"killed": ["m.a"], status: ["m.b"]})

    assert report["status"] == "regression"
    assert report["fatal_outcomes"] == {status: ["m.b"]}


def test_negative_control_turns_a_floor_owner_red(pilot: ModuleType) -> None:
    owner = _owner(pilot, floor=0.9)
    outcomes = {"killed": ["m.a", "m.b"], "timeout": ["m.c"]}

    assert pilot.owner_report(owner, outcomes)["status"] == "accepted"
    report = pilot.negative_control(owner, outcomes)

    assert report["status"] == "regression"
    assert report["measured"]["kill_rate"] == 0.0
    assert report["unlisted_survivors"] == ["m.a", "m.b", "m.c"]


def test_main_negative_control_exit_code(
    pilot: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    floors = tmp_path / "floors.json"
    floors.write_text(json.dumps(_payload()), encoding="utf-8")
    monkeypatch.setattr(pilot, "_load_exit_codes", lambda owner, root: {"m.a__mutmut_1": 1})

    assert pilot.main(["--floors", str(floors), "--negative-control", "demo"]) == 0

    zero_floor = _payload(demo=_entry(floor=0.0))
    floors.write_text(json.dumps(zero_floor), encoding="utf-8")
    assert pilot.main(["--floors", str(floors), "--negative-control", "demo"]) == 1


def test_main_fails_when_a_kill_rate_drops_below_floor(
    pilot: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    floors = tmp_path / "floors.json"
    report = tmp_path / "report.json"
    floors.write_text(json.dumps(_payload()), encoding="utf-8")
    monkeypatch.setattr(
        pilot,
        "_load_exit_codes",
        lambda owner, root: {"m.fn__mutmut_1": 1, "m.fn__mutmut_2": 0},
    )
    monkeypatch.setattr(pilot, "_run_mutmut", lambda *a, **k: pytest.fail("ran mutmut"))

    result = pilot.main(["--floors", str(floors), "--report-only", "--output", str(report)])

    assert result == 1
    written = json.loads(report.read_text(encoding="utf-8"))
    assert written["status"] == "regression"
    assert written["owners"]["demo"]["measured"]["kill_rate"] == 0.5
    assert "[x] demo: kill_rate=0.5 floor=1.0" in capsys.readouterr().out


def test_update_floors_raises_but_never_lowers(pilot: ModuleType) -> None:
    payload = _payload(high=_entry(floor=0.9), low=_entry(floor=0.1))
    reports = {
        "high": {"fatal_outcomes": {}, "measured": {"kill_rate": 0.5}},
        "low": {"fatal_outcomes": {}, "measured": {"kill_rate": 0.8765}},
    }

    updated = pilot.updated_floors_payload(payload, reports)

    assert updated["owners"]["high"]["floor"] == 0.9
    assert updated["owners"]["low"]["floor"] == 0.87
    assert updated["owners"]["low"]["measured"] == {"kill_rate": 0.8765}
    assert payload["owners"]["low"]["floor"] == 0.1


def test_update_floors_refuses_incomplete_evidence(pilot: ModuleType) -> None:
    reports = {"demo": {"fatal_outcomes": {"suspicious": ["m.a"]}, "measured": {}}}

    with pytest.raises(pilot.PilotError, match="non-survivor failures"):
        pilot.updated_floors_payload(_payload(), reports)


@pytest.mark.parametrize(("rate", "floor"), [(1.0, 1.0), (0.999, 0.99), (0.5, 0.5)])
def test_truncated_floor_never_rounds_up(pilot: ModuleType, rate: float, floor: float) -> None:
    assert pilot.truncated_floor(rate) == floor


# --- ratchet -------------------------------------------------------------------


def test_ratchet_rejects_lowered_floor_and_removed_owner(pilot: ModuleType) -> None:
    base = _payload(kept=_entry(floor=0.9), gone=_entry(floor=0.5))
    current = _payload(kept=_entry(floor=0.8))

    assert pilot.check_floor_ratchet(current, base) == [
        "gone: owner removed from mutation floors",
        "kept: floor lowered from 0.9 to 0.8",
    ]


def test_ratchet_allows_raised_floor_remap_and_first_introduction(pilot: ModuleType) -> None:
    base = _payload(kept=_entry(floor=0.8))
    remapped = _entry(floor=0.85)
    remapped["architecture_owner"] = "scope-manifest-path"
    current = _payload(kept=remapped)

    assert pilot.check_floor_ratchet(current, base) == []
    assert pilot.check_floor_ratchet(current, None) == []


def test_main_ratchet_against_git_ref(
    pilot: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    floors = tmp_path / "floors.json"
    floors.write_text(json.dumps(_payload(demo=_entry(floor=0.5))), encoding="utf-8")
    monkeypatch.setattr(
        pilot,
        "load_baseline_at_ref",
        lambda root, path, ref, label: _payload(demo=_entry(floor=0.6)),
    )

    args = ["--floors", str(floors), "--check-floor-ratchet", "--base-ref", "abc"]
    assert pilot.main(args) == 1
    assert pilot.main(["--floors", str(floors), "--check-floor-ratchet"]) == 0


# --- execution plumbing --------------------------------------------------------


def test_sanitized_environment_removes_credentials_and_preserves_safe_values(
    pilot: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {name: "secret" for name in pilot.SENSITIVE_ENVIRONMENT_NAMES}
    environment["HOME"] = "/safe/home"
    monkeypatch.setattr(pilot.os, "environ", environment)

    assert pilot._sanitized_environment() == {"HOME": "/safe/home"}


@pytest.mark.parametrize(("reuse_cache", "expected_removals"), [(False, 1), (True, 0)])
def test_run_mutmut_respects_cache_mode_and_passes_owner_patterns(
    pilot: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reuse_cache: bool,
    expected_removals: int,
) -> None:
    removals = []
    monkeypatch.setattr(
        pilot.shutil, "rmtree", lambda path, ignore_errors: removals.append(path)
    )
    monkeypatch.setattr(pilot.shutil, "which", lambda name: f"/venv/bin/{name}")
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(pilot.subprocess, "run", run)
    owner = _owner(pilot)

    pilot._run_mutmut((owner,), max_children=3, reuse_cache=reuse_cache, repo_root=tmp_path)

    assert len(removals) == expected_removals
    assert run.call_args.args[0] == [
        "/venv/bin/mutmut",
        "run",
        "--max-children",
        "3",
        *owner.patterns,
    ]


def test_run_mutmut_failure_is_reported(
    pilot: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(pilot.shutil, "which", lambda name: f"/venv/bin/{name}")
    monkeypatch.setattr(
        pilot.subprocess,
        "run",
        Mock(return_value=SimpleNamespace(returncode=2, stdout="", stderr="boom")),
    )

    with pytest.raises(pilot.PilotError, match="exit code 2"):
        pilot._run_mutmut((_owner(pilot),), max_children=1, reuse_cache=True, repo_root=tmp_path)


def _write_meta(repo_root: Path, payload: object, *, raw: bytes | None = None) -> None:
    path = repo_root / "mutants" / "src/apm_cli/x/mod.py.meta"
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        path.write_bytes(raw)
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize(
    ("payload", "raw", "message"),
    [
        (None, b"{not json", "invalid mutmut metadata"),
        (None, b"\xff", "invalid mutmut metadata"),
        ([], None, "root must be an object"),
        ({"other": {}}, None, "exit_code_by_key missing"),
        ({"exit_code_by_key": {"apm_cli.x.mod.x_fn__mutmut_1": "0"}}, None, "invalid exit code"),
        ({"exit_code_by_key": {"apm_cli.x.mod.x_fn__mutmut_1": True}}, None, "invalid exit code"),
        ({"exit_code_by_key": {"apm_cli.other.x_fn__mutmut_1": 0}}, None, "no mutants matched"),
    ],
)
def test_load_exit_codes_fails_closed(
    pilot: ModuleType, tmp_path: Path, payload: object, raw: bytes | None, message: str
) -> None:
    _write_meta(tmp_path, payload, raw=raw)

    with pytest.raises(pilot.PilotError, match=message):
        pilot._load_exit_codes(_owner(pilot), tmp_path)


def test_load_exit_codes_scopes_to_owner_patterns(pilot: ModuleType, tmp_path: Path) -> None:
    separator = chr(0x01C1)
    _write_meta(
        tmp_path,
        {
            "exit_code_by_key": {
                "apm_cli.x.mod.x_fn__mutmut_2": 0,
                f"apm_cli.x.mod.x{separator}Klass{separator}method__mutmut_1": 1,
                "apm_cli.x.mod.x_fn_other__mutmut_1": 1,
                "apm_cli.x.mod.x_unrelated__mutmut_1": None,
            }
        },
    )

    assert pilot._load_exit_codes(_owner(pilot), tmp_path) == {
        "apm_cli.x.mod.fn__mutmut_2": 0,
        "apm_cli.x.mod.Klass.method__mutmut_1": 1,
    }


@pytest.mark.windows_compat
def test_write_report_is_deterministic_atomic_and_printable_ascii(
    pilot: ModuleType, tmp_path: Path
) -> None:
    report_path = tmp_path / "nested" / "report.json"
    payload = {"z": "snowman \u2603", "a": {"z": 2, "a": 1}}

    pilot._write_report(report_path, payload)
    first = report_path.read_bytes()
    pilot._write_report(report_path, {"a": {"a": 1, "z": 2}, "z": "snowman \u2603"})

    assert report_path.read_bytes() == first
    assert all(byte < 128 for byte in first)
    assert first == (
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    ).encode("ascii")
    assert not (report_path.parent / f".{report_path.name}.tmp").exists()


@pytest.mark.windows_compat
def test_write_report_preserves_existing_file_when_atomic_replace_fails(
    pilot: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text("previous\n", encoding="ascii")

    def fail_replace(source: str, target: str) -> None:
        raise OSError("replace failed")

    # _write_report routes through apm_cli.utils.atomic_io, which renames via os.replace.
    monkeypatch.setattr("apm_cli.utils.atomic_io.os.replace", fail_replace)

    with pytest.raises(pilot.PilotError, match="failed to write mutation report"):
        pilot._write_report(report_path, {"status": "accepted"})

    assert not (report_path.parent / f".{report_path.name}.tmp").exists()
    assert report_path.read_text(encoding="ascii") == "previous\n"
