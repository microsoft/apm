from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

RATCHET_TEST_SCOPE = "fixture"

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check_exact_test_duplicates.py"
ALGORITHM_DOC = json.loads(
    (REPO_ROOT / "tests" / "quality" / "exact_test_duplicates.json").read_text(encoding="utf-8")
)["algorithm_doc"]

BODY = "def test_value():\n    assert 1 == 1\n"


def _run(root: Path, baseline: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--root",
            str(root),
            "--baseline",
            str(baseline),
            *extra,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _payload(
    bodies: dict[str, list[str]] | None = None,
    names: dict[str, list[str]] | None = None,
) -> dict[str, object]:
    return {
        "algorithm": "ast-body-v1",
        "algorithm_doc": ALGORITHM_DOC,
        "duplicate_bodies": bodies or {},
        "duplicate_names": names or {},
        "schema_version": 2,
        "scope": ["tests/**/test_*.py", "tests/**/*_test.py"],
    }


def _write_baseline(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write(root: Path, name: str, content: str) -> Path:
    path = root / "tests" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _track_tests(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "tests"], check=True)


def test_seeded_duplicate_body_fails_naming_file_and_fix(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write(root, "test_a.py", BODY)
    _write(
        root,
        "test_b.py",
        'def test_other_name():\n    """Docstrings and comments are ignored."""\n'
        "    # comment\n    assert 1 == 1\n",
    )
    _track_tests(root)
    baseline = root / "baseline.json"
    _write_baseline(baseline, _payload())

    result = _run(root, baseline)

    assert result.returncode == 1
    assert result.stdout == ""
    assert "[x] tests/test_a.py: test test_value duplicates the body (ast-body-v1 " in (
        result.stderr
    )
    assert "[x] tests/test_b.py: test test_other_name duplicates the body" in result.stderr
    assert "Fix: delete the copy, or parametrize/differentiate it" in result.stderr


def test_different_literal_is_not_a_duplicate(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write(root, "test_a.py", BODY)
    _write(root, "test_b.py", "def test_value():\n    assert 1 == 2\n")
    _track_tests(root)
    baseline = root / "baseline.json"
    _write_baseline(baseline, _payload())

    result = _run(root, baseline)

    assert result.returncode == 0, result.stderr


def test_class_name_reused_across_files_fails(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write(root, "test_a.py", "class TestX:\n    def test_y(self):\n        assert 1\n")
    _write(root, "test_b.py", "class TestX:\n    def test_y(self):\n        assert 2\n")
    _track_tests(root)
    baseline = root / "baseline.json"
    _write_baseline(baseline, _payload(names={"tests/test_a.py": ["TestX::test_y"]}))

    result = _run(root, baseline)

    assert result.returncode == 1
    assert (
        "[x] tests/test_b.py: test TestX::test_y reuses a Class::name already defined "
        "in tests/test_a.py. Fix: rename the class or test"
    ) in result.stderr
    assert "tests/test_a.py: test" not in result.stderr


PAIR = {"tests/test_a.py": ["test_value"], "tests/test_b.py": ["test_value"]}


def test_third_copy_exceeds_baselined_pair(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    for name in ("test_a.py", "test_b.py", "test_c.py"):
        _write(root, name, BODY)
    _track_tests(root)
    baseline = root / "baseline.json"
    _write_baseline(baseline, _payload(bodies=PAIR))

    result = _run(root, baseline)

    assert result.returncode == 1
    assert "[x] tests/test_c.py: test test_value duplicates the body" in result.stderr
    assert "of tests/test_a.py::test_value, tests/test_b.py::test_value" in result.stderr
    assert "[x] tests/test_a.py" not in result.stderr


def test_lockstep_edit_of_baselined_pair_passes(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    edited = "def test_value():\n    assert 2 == 2\n"
    for name in ("test_a.py", "test_b.py"):
        _write(root, name, edited)
    _track_tests(root)
    baseline = root / "baseline.json"
    _write_baseline(baseline, _payload(bodies=PAIR))

    result = _run(root, baseline)

    assert result.returncode == 0, result.stderr
    assert "[i]" not in result.stdout


def test_update_refuses_growth_without_writing(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write(root, "test_a.py", BODY)
    _write(root, "test_b.py", BODY)
    _track_tests(root)
    baseline = root / "baseline.json"
    _write_baseline(baseline, _payload())
    before = baseline.read_bytes()

    result = _run(root, baseline, "--update-baseline")

    assert result.returncode == 1
    assert "refusing to update test-duplicate baseline with new duplicates" in result.stderr
    assert baseline.read_bytes() == before


def test_stale_entries_are_info_and_update_prunes_them(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write(root, "test_a.py", BODY)
    _write(root, "test_b.py", BODY)
    _track_tests(root)
    baseline = root / "baseline.json"
    _write_baseline(
        baseline,
        _payload(
            bodies={**PAIR, "tests/test_gone.py": ["test_value"]},
            names={"tests/test_gone.py": ["TestGone::test_value"]},
        ),
    )

    stale = _run(root, baseline)
    updated = _run(root, baseline, "--update-baseline")

    assert stale.returncode == 0
    assert stale.stderr == ""
    assert (
        "[i] stale test-duplicate baseline entry (no longer duplicated): "
        "duplicate_bodies tests/test_gone.py::test_value"
    ) in stale.stdout
    assert "duplicate_names tests/test_gone.py::TestGone::test_value" in stale.stdout
    assert updated.returncode == 0
    assert (
        "[+] updated test-duplicate baseline: removed 2 stale and re-keyed 0 moved entries"
        in updated.stdout
    )
    assert json.loads(baseline.read_text(encoding="utf-8")) == _payload(bodies=PAIR)


def test_moved_baselined_duplicate_is_rekeyed_not_new(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    named = "class TestX:\n    def test_value(self):\n        assert 1 == 1\n"
    _write(root, "test_a.py", named)
    _write(root, "integration/test_b.py", named)
    _track_tests(root)
    baseline = root / "baseline.json"
    _write_baseline(
        baseline,
        _payload(
            bodies={
                "tests/test_a.py": ["TestX::test_value"],
                "tests/test_b.py": ["TestX::test_value"],
            },
            names={
                "tests/test_a.py": ["TestX::test_value"],
                "tests/test_b.py": ["TestX::test_value"],
            },
        ),
    )

    moved = _run(root, baseline)
    updated = _run(root, baseline, "--update-baseline")

    assert moved.returncode == 0, moved.stderr
    assert (
        "[i] moved test-duplicate baseline entry: duplicate_bodies tests/test_b.py::TestX::"
        "test_value -> duplicate_bodies tests/integration/test_b.py::TestX::test_value"
    ) in moved.stdout
    assert updated.returncode == 0
    rekeyed = {
        "tests/integration/test_b.py": ["TestX::test_value"],
        "tests/test_a.py": ["TestX::test_value"],
    }
    assert json.loads(baseline.read_text(encoding="utf-8")) == _payload(
        bodies=rekeyed, names=rekeyed
    )


@pytest.mark.parametrize(
    "mutation",
    [
        {"duplicate_bodies": {"tests/test_a.py": {"test_value": "abc"}}},
        {"duplicate_bodies": {"tests/test_a.py": []}},
        {"duplicate_names": {"tests/test_a.py": ["b::c", "a::c"]}},
        {"algorithm": "sha256-bytes-v1"},
        {"algorithm_doc": "undocumented"},
        {"schema_version": 1},
    ],
)
def test_malformed_baseline_fails_closed(tmp_path: Path, mutation: dict[str, object]) -> None:
    root = tmp_path / "repo"
    baseline = root / "baseline.json"
    _write_baseline(baseline, {**_payload(), **mutation})

    result = _run(root, baseline)

    assert result.returncode == 2
    assert result.stdout == ""
    assert "[x] invalid test-duplicate baseline" in result.stderr


def test_tracked_symlink_inside_root_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    target = _write(root, "fixture.py", BODY)
    link = root / "tests" / "test_link.py"
    try:
        link.symlink_to(target.name)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")
    _track_tests(root)
    baseline = root / "baseline.json"
    _write_baseline(baseline, _payload())

    result = _run(root, baseline)

    assert result.returncode == 2
    assert "tracked Python path is a symlink: tests/test_link.py" in result.stderr


def test_symlink_outside_root_fails_containment_check(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    tests = root / "tests"
    tests.mkdir(parents=True)
    outside = tmp_path / "outside.py"
    outside.write_text(BODY, encoding="utf-8")
    link = tests / "test_escape.py"
    try:
        link.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")
    _track_tests(root)
    baseline = root / "baseline.json"
    _write_baseline(baseline, _payload())

    result = _run(root, baseline)

    assert result.returncode == 2
    assert "tracked Python path resolves outside repository" in result.stderr


def test_worktree_edits_are_hashed_without_staging(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write(root, "test_a.py", BODY)
    second = _write(root, "test_b.py", "def test_value():\n    assert 2 == 2\n")
    _track_tests(root)
    second.write_text(BODY, encoding="utf-8")
    baseline = root / "baseline.json"
    _write_baseline(baseline, _payload())

    result = _run(root, baseline)

    assert result.returncode == 1
    assert "duplicates the body" in result.stderr


def test_untracked_duplicate_is_out_of_scope(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write(root, "test_tracked.py", BODY)
    _track_tests(root)
    _write(root, "test_untracked.py", BODY)
    baseline = root / "baseline.json"
    _write_baseline(baseline, _payload())

    result = _run(root, baseline)

    assert result.returncode == 0
    assert "1 files, 1 tests, 0 duplicate body group(s)" in result.stdout


def test_provisional_baseline_requires_explicit_allow(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write(root, "test_tracked.py", BODY)
    _track_tests(root)
    baseline = root / "baseline.json"
    payload = _payload()
    payload["provisional"] = {
        "basis_commit": "abc123",
        "required_follow_up": "remeasure",
    }
    _write_baseline(baseline, payload)

    final = _run(root, baseline)
    provisional = _run(root, baseline, "--allow-provisional")
    del payload["provisional"]
    _write_baseline(baseline, payload)
    finalized = _run(root, baseline)

    assert final.returncode == 2
    assert "provisional baseline is not allowed in final mode" in final.stderr
    assert provisional.returncode == 0
    assert provisional.stderr == ""
    assert finalized.returncode == 0
    assert finalized.stderr == ""


def test_unique_but_present_test_is_not_a_move_source(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    named = "class TestX:\n    def test_value(self):\n        assert 1 == 1\n"
    _write(root, "test_a.py", named.replace("1 == 1", "2 == 2"))
    _write(root, "test_b.py", named)
    _write(root, "integration/test_c.py", named)
    _track_tests(root)
    baseline = root / "baseline.json"
    pair = {"tests/test_a.py": ["TestX::test_value"], "tests/test_b.py": ["TestX::test_value"]}
    _write_baseline(baseline, _payload(bodies=pair))

    result = _run(root, baseline)

    assert result.returncode == 1
    assert "[x] tests/integration/test_c.py: test TestX::test_value duplicates the body" in (
        result.stderr
    )
    assert "moved" not in result.stdout
