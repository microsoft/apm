from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

RATCHET_TEST_SCOPE = "fixture"

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check_exact_test_duplicates.py"
sys.path.insert(0, str(SCRIPT.parent))
from check_exact_test_duplicates import ALGORITHM_DESCRIPTION  # noqa: E402

BODY = b"def test_value():\n    assert 1 == 1\n"
CLASS_A = b"class TestThing:\n    def test_value(self):\n        assert 'a' == 'a'\n"
CLASS_B = b"class TestThing:\n    def test_value(self):\n        assert 'b' == 'b'\n"


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
    bodies: list[list[str]] | None = None,
    names: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "algorithm": "ast-body-v2",
        "algorithm_description": ALGORITHM_DESCRIPTION,
        "body_duplicate_groups": bodies or [],
        "name_collision_groups": names or [],
        "schema_version": 3,
        "scope": ["tests/**/test_*.py", "tests/**/*_test.py"],
    }


def _write_baseline(
    path: Path,
    bodies: list[list[str]] | None = None,
    names: list[dict[str, object]] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_payload(bodies, names), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_bytes(root: Path, name: str, content: bytes) -> Path:
    path = root / "tests" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _track_tests(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "tests"], check=True)


def _repo(tmp_path: Path, files: dict[str, bytes]) -> Path:
    root = tmp_path / "repo"
    for name, content in files.items():
        _write_bytes(root, name, content)
    _track_tests(root)
    return root


def test_seeded_duplicate_body_fails_naming_tests_and_fix(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "test_a.py": BODY,
            "test_b.py": b"def test_other():\n    assert 2 == 2\n\n\n"
            b"def test_copy():\n    assert 1 == 1\n",
        },
    )
    baseline = root / "baseline.json"
    _write_baseline(baseline)

    result = _run(root, baseline)

    assert result.returncode == 1
    assert result.stdout == ""
    assert "duplicate test body ast-body-v2:" in result.stderr
    assert "  - tests/test_a.py::test_value (new)" in result.stderr
    assert "  - tests/test_b.py::test_copy (new)" in result.stderr
    assert "test_other" not in result.stderr
    assert "Fix: delete the redundant copy" in result.stderr


def test_duplicate_bodies_inside_one_file_fail(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "test_a.py": CLASS_A
            + b"\n\nclass TestCopy:\n    def test_again(self):\n        assert 'a' == 'a'\n"
        },
    )
    baseline = root / "baseline.json"
    _write_baseline(baseline)

    result = _run(root, baseline)

    assert result.returncode == 1
    assert "  - tests/test_a.py::TestCopy::test_again (new)" in result.stderr


def test_formatting_docstrings_and_comments_are_normalized(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "test_lf.py": BODY,
            "test_crlf.py": b'def test_other():\r\n    """Doc."""\r\n'
            b"    # comment\r\n    assert 1  ==  1\r\n",
        },
    )
    baseline = root / "baseline.json"
    _write_baseline(baseline)

    result = _run(root, baseline)

    assert result.returncode == 1
    assert "tests/test_crlf.py::test_other (new)" in result.stderr


@pytest.mark.parametrize(
    "other",
    [
        b"def test_value():\n    assert 1 == 2\n",
        b"import pytest\n\n@pytest.mark.parametrize('x', [2])\n"
        b"def test_value(x):\n    assert 1 == 1\n",
        b"def test_value(tmp_path):\n    assert 1 == 1\n",
        b"def helper_value():\n    assert 1 == 1\n",
    ],
)
def test_distinct_tests_pass(tmp_path: Path, other: bytes) -> None:
    root = _repo(tmp_path, {"test_a.py": BODY, "test_b.py": other})
    baseline = root / "baseline.json"
    _write_baseline(baseline)

    result = _run(root, baseline)

    assert result.returncode == 0, result.stderr
    assert "0 allowed body duplicate group(s)" in result.stdout


def test_same_class_test_name_in_one_directory_fails(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"test_a.py": CLASS_A, "test_b.py": CLASS_B})
    baseline = root / "baseline.json"
    _write_baseline(baseline)

    result = _run(root, baseline)

    assert result.returncode == 1
    assert (
        "test name tests/::TestThing::test_value is defined in more than one file of one directory"
    ) in result.stderr
    assert "  - tests/test_a.py (new)" in result.stderr
    assert "  - tests/test_b.py (new)" in result.stderr
    assert "Fix: rename the new class or test" in result.stderr


def test_baselined_groups_pass_and_report_counts(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {"test_a.py": BODY, "test_b.py": BODY, "test_c.py": CLASS_A, "test_d.py": CLASS_B},
    )
    baseline = root / "baseline.json"
    _write_baseline(
        baseline,
        [["tests/test_a.py::test_value", "tests/test_b.py::test_value"]],
        [
            {
                "name": "tests/::TestThing::test_value",
                "paths": ["tests/test_c.py", "tests/test_d.py"],
            }
        ],
    )

    result = _run(root, baseline)

    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "[+] exact test duplicate ratchet clean: 4 files, 4 tests, "
        "1 allowed body duplicate group(s), 1 allowed name collision group(s)\n"
    )


def test_tandem_edit_of_baselined_group_passes(tmp_path: Path) -> None:
    edited = b"def test_value():\n    assert 3 == 3\n"
    root = _repo(tmp_path, {"test_a.py": edited, "test_b.py": edited})
    baseline = root / "baseline.json"
    _write_baseline(baseline, [["tests/test_a.py::test_value", "tests/test_b.py::test_value"]])

    result = _run(root, baseline)

    assert result.returncode == 0, result.stderr


def test_third_copy_exceeds_pair_baseline(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"test_a.py": BODY, "test_b.py": BODY, "test_c.py": BODY})
    baseline = root / "baseline.json"
    _write_baseline(baseline, [["tests/test_a.py::test_value", "tests/test_b.py::test_value"]])

    result = _run(root, baseline)

    assert result.returncode == 1
    assert "  - tests/test_a.py::test_value\n" in result.stderr
    assert "  - tests/test_c.py::test_value (new)" in result.stderr


def test_merging_two_baselined_groups_fails(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {"test_a.py": BODY, "test_b.py": BODY, "test_c.py": BODY, "test_d.py": BODY},
    )
    baseline = root / "baseline.json"
    _write_baseline(
        baseline,
        [
            ["tests/test_a.py::test_value", "tests/test_b.py::test_value"],
            ["tests/test_c.py::test_value", "tests/test_d.py::test_value"],
        ],
    )

    result = _run(root, baseline)

    assert result.returncode == 1
    assert "is new or grew" in result.stderr


def test_update_refuses_growth_without_writing(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"test_a.py": BODY, "test_b.py": BODY})
    baseline = root / "baseline.json"
    _write_baseline(baseline)
    before = baseline.read_bytes()

    result = _run(root, baseline, "--update-baseline")

    assert result.returncode == 1
    assert "refusing to update exact-duplicate baseline with new debt" in result.stderr
    assert baseline.read_bytes() == before


def test_reduction_requires_baseline_tightening(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"test_a.py": BODY, "test_c.py": CLASS_A})
    baseline = root / "baseline.json"
    _write_baseline(
        baseline,
        [["tests/test_a.py::test_value", "tests/test_b.py::test_value"]],
        [
            {
                "name": "tests/::TestThing::test_value",
                "paths": ["tests/test_c.py", "tests/test_d.py"],
            }
        ],
    )

    stale = _run(root, baseline)
    updated = _run(root, baseline, "--update-baseline")

    assert stale.returncode == 1
    assert "body duplicate group reduced:" in stale.stderr
    assert "name collision group tests/::TestThing::test_value reduced" in stale.stderr
    assert (
        "uv run --frozen python scripts/check_exact_test_duplicates.py --update-baseline"
    ) in stale.stderr
    assert updated.returncode == 0
    assert (
        "[+] updated exact-duplicate baseline: 1 -> 0 body group(s), 1 -> 0 name group(s)"
    ) in updated.stdout
    assert json.loads(baseline.read_text(encoding="utf-8")) == _payload()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(body_duplicate_groups=[["b::t", "a::t"]]),
        lambda payload: payload.update(body_duplicate_groups=[["a::t", "b::t"], ["a::t", "c::t"]]),
        lambda payload: payload.update(
            name_collision_groups=[{"name": "a/::T::t", "paths": ["a/x"]}]
        ),
        lambda payload: payload.update(
            name_collision_groups=[{"name": "a/::T::t", "paths": ["a/x", "b/y"]}]
        ),
        lambda payload: payload.update(algorithm_description="anything"),
        lambda payload: payload.update(algorithm="ast-body-v1", schema_version=2),
    ],
)
def test_malformed_baseline_fails_closed(tmp_path: Path, mutate: object) -> None:
    root = tmp_path / "repo"
    baseline = root / "baseline.json"
    payload = _payload()
    mutate(payload)  # type: ignore[operator]
    root.mkdir()
    baseline.write_text(json.dumps(payload), encoding="utf-8")

    result = _run(root, baseline)

    assert result.returncode == 2
    assert result.stdout == ""
    assert "[x] invalid exact-duplicate baseline" in result.stderr


def test_unparseable_test_module_fails_closed(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"test_a.py": b"def test_value(:\n"})
    baseline = root / "baseline.json"
    _write_baseline(baseline)

    result = _run(root, baseline)

    assert result.returncode == 2
    assert "[x] exact-duplicate scan failed" in result.stderr


def test_tracked_symlink_inside_root_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    target = _write_bytes(root, "fixture.py", BODY)
    link = root / "tests" / "test_link.py"
    try:
        link.symlink_to(target.name)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")
    _track_tests(root)
    baseline = root / "baseline.json"
    _write_baseline(baseline)

    result = _run(root, baseline)

    assert result.returncode == 2
    assert "tracked Python path is a symlink: tests/test_link.py" in result.stderr


def test_symlink_outside_root_fails_containment_check(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    tests = root / "tests"
    tests.mkdir(parents=True)
    outside = tmp_path / "outside.py"
    outside.write_bytes(BODY)
    link = tests / "test_escape.py"
    try:
        link.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")
    _track_tests(root)
    baseline = root / "baseline.json"
    _write_baseline(baseline)

    result = _run(root, baseline)

    assert result.returncode == 2
    assert "tracked Python path resolves outside repository" in result.stderr


def test_worktree_edits_are_scanned_without_staging(tmp_path: Path) -> None:
    root = _repo(
        tmp_path, {"test_a.py": BODY, "test_b.py": b"def test_value():\n    assert 2 == 2\n"}
    )
    (root / "tests" / "test_b.py").write_bytes(BODY)
    baseline = root / "baseline.json"
    _write_baseline(baseline)

    result = _run(root, baseline)

    assert result.returncode == 1
    assert "duplicate test body" in result.stderr


def test_untracked_duplicate_is_out_of_scope(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"test_tracked.py": BODY})
    _write_bytes(root, "test_untracked.py", BODY)
    baseline = root / "baseline.json"
    _write_baseline(baseline)

    result = _run(root, baseline)

    assert result.returncode == 0
    assert "1 files, 1 tests, 0 allowed body duplicate group(s)" in result.stdout


def test_provisional_baseline_requires_explicit_allow(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"test_tracked.py": BODY})
    baseline = root / "baseline.json"
    payload = _payload()
    payload["provisional"] = {
        "basis_commit": "abc123",
        "required_follow_up": "remeasure",
    }
    baseline.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    final = _run(root, baseline)
    provisional = _run(root, baseline, "--allow-provisional")
    del payload["provisional"]
    baseline.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    finalized = _run(root, baseline)

    assert final.returncode == 2
    assert "provisional baseline is not allowed in final mode" in final.stderr
    assert provisional.returncode == 0
    assert provisional.stderr == ""
    assert finalized.returncode == 0
    assert finalized.stderr == ""


def test_same_class_test_name_in_other_directories_passes(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"test_a.py": CLASS_A, "unit/test_b.py": CLASS_B})
    baseline = root / "baseline.json"
    _write_baseline(baseline)

    result = _run(root, baseline)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            b"class TestCopilot:\n    target = 'copilot'\n\n"
            b"    def test_value(self):\n        assert self.target\n",
            b"class TestClaude:\n    target = 'claude'\n\n"
            b"    def test_value(self):\n        assert self.target\n",
        ),
        (
            b"TARGET = 'copilot'\n\ndef test_value():\n    assert TARGET\n",
            b"TARGET = 'claude'\n\ndef test_value():\n    assert TARGET\n",
        ),
    ],
    ids=["class-level-target", "module-level-constant"],
)
def test_context_assignments_distinguish_identical_bodies(
    tmp_path: Path, first: bytes, second: bytes
) -> None:
    root = _repo(tmp_path, {"test_a.py": first, "test_b.py": second})
    baseline = root / "baseline.json"
    _write_baseline(baseline)

    result = _run(root, baseline)

    assert result.returncode == 0, result.stderr


def test_renamed_locals_are_still_duplicates(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "test_a.py": b"def test_value(tmp_path):\n    total = [n for n in range(3)]\n"
            b"    for item in total:\n        assert item >= 0\n",
            "test_b.py": b"def test_other(tmp_path):\n    values = [k for k in range(3)]\n"
            b"    for entry in values:\n        assert entry >= 0\n",
        },
    )
    baseline = root / "baseline.json"
    _write_baseline(baseline)

    result = _run(root, baseline)

    assert result.returncode == 1
    assert "tests/test_b.py::test_other (new)" in result.stderr


def test_pure_move_of_baselined_group_needs_only_tightening(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"test_c.py": BODY, "test_d.py": BODY})
    baseline = root / "baseline.json"
    _write_baseline(baseline, [["tests/test_a.py::test_value", "tests/test_b.py::test_value"]])

    moved = _run(root, baseline)
    updated = _run(root, baseline, "--update-baseline")

    assert moved.returncode == 1
    assert "is new or grew" not in moved.stderr
    assert "body duplicate group moved or removed:" in moved.stderr
    assert updated.returncode == 0, updated.stderr
    assert json.loads(baseline.read_text(encoding="utf-8")) == _payload(
        [["tests/test_c.py::test_value", "tests/test_d.py::test_value"]]
    )


def test_move_that_grows_the_group_fails_with_hint(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"test_c.py": BODY, "test_d.py": BODY, "test_e.py": BODY})
    baseline = root / "baseline.json"
    _write_baseline(baseline, [["tests/test_a.py::test_value", "tests/test_b.py::test_value"]])

    result = _run(root, baseline)

    assert result.returncode == 1
    assert "is new or grew" in result.stderr
    assert "If you only moved or renamed baselined copies" in result.stderr


def test_baseline_growth_against_merge_base_fails(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"test_a.py": BODY, "test_b.py": BODY})
    baseline = root / "baseline.json"
    base = root / "base.json"
    _write_baseline(baseline, [["tests/test_a.py::test_value", "tests/test_b.py::test_value"]])
    _write_baseline(base)

    result = _run(root, baseline, "--base-baseline", str(base))

    assert result.returncode == 1
    assert "baseline grew relative to the merge-base" in result.stderr
    assert "may only shrink" in result.stderr


def test_baseline_shrink_or_algorithm_change_against_merge_base_passes(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path, {"test_a.py": BODY, "test_b.py": BODY})
    group = [["tests/test_a.py::test_value", "tests/test_b.py::test_value"]]
    baseline = root / "baseline.json"
    base = root / "base.json"
    _write_baseline(baseline, group)
    _write_baseline(base, [*group, ["tests/test_x.py::test_x", "tests/test_y.py::test_y"]])
    old = root / "old.json"
    old.write_text(json.dumps({"algorithm": "sha256-bytes-v1"}), encoding="utf-8")

    shrunk = _run(root, baseline, "--base-baseline", str(base))
    reseeded = _run(root, baseline, "--base-baseline", str(old))

    assert shrunk.returncode == 0, shrunk.stderr
    assert reseeded.returncode == 0, reseeded.stderr
    assert "monotonicity starts from this baseline" in reseeded.stdout
