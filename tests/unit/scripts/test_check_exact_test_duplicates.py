from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

RATCHET_TEST_SCOPE = "fixture"

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check_exact_test_duplicates.py"


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


def _payload(groups: list[dict[str, object]]) -> dict[str, object]:
    return {
        "algorithm": "sha256-bytes-v1",
        "duplicate_groups": groups,
        "schema_version": 1,
        "scope": ["tests/**/test_*.py", "tests/**/*_test.py"],
    }


def _write_baseline(path: Path, groups: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_payload(groups), indent=2, sort_keys=True) + "\n",
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


def test_new_exact_pair_fails(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    content = b"def test_value():\n    assert 1 == 1\n"
    _write_bytes(root, "test_a.py", content)
    _write_bytes(root, "test_b.py", content)
    _track_tests(root)
    baseline = root / "baseline.json"
    _write_baseline(baseline, [])
    digest = hashlib.sha256(content).hexdigest()

    result = _run(root, baseline)

    assert result.returncode == 1
    assert result.stdout == ""
    assert f"new exact duplicate group {digest}" in result.stderr
    assert "  - tests/test_a.py" in result.stderr
    assert "  - tests/test_b.py" in result.stderr
    assert "make each test module meaningfully distinct" in result.stderr


def test_near_duplicate_pair_passes(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_bytes(root, "test_a.py", b"def test_value():\n    assert 1 == 1\n")
    _write_bytes(root, "test_b.py", b"def test_value():\n    assert 1 == 2\n")
    _track_tests(root)
    baseline = root / "baseline.json"
    _write_baseline(baseline, [])

    result = _run(root, baseline)

    assert result.returncode == 0


def test_crlf_and_lf_are_not_exact_duplicates(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_bytes(root, "test_lf.py", b"def test_value():\n    assert 1 == 1\n")
    _write_bytes(
        root,
        "test_crlf.py",
        b"def test_value():\r\n    assert 1 == 1\r\n",
    )
    _track_tests(root)
    baseline = root / "baseline.json"
    _write_baseline(baseline, [])

    result = _run(root, baseline)

    assert result.returncode == 0


def test_third_copy_exceeds_pair_baseline(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    content = b"def test_value():\n    assert 1 == 1\n"
    digest = hashlib.sha256(content).hexdigest()
    for name in ("test_a.py", "test_b.py", "test_c.py"):
        _write_bytes(root, name, content)
    _track_tests(root)
    baseline = root / "baseline.json"
    _write_baseline(
        baseline,
        [
            {
                "paths": ["tests/test_a.py", "tests/test_b.py"],
                "sha256": digest,
            }
        ],
    )

    result = _run(root, baseline)

    assert result.returncode == 1
    assert f"exact duplicate group {digest} added tracked path(s)" in result.stderr
    assert "  - tests/test_c.py" in result.stderr
    assert "remove or differentiate the added copy" in result.stderr


def test_update_refuses_existing_group_growth_without_writing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    content = b"def test_value():\n    assert 1 == 1\n"
    digest = hashlib.sha256(content).hexdigest()
    for name in ("test_a.py", "test_b.py", "test_c.py"):
        _write_bytes(root, name, content)
    _track_tests(root)
    baseline = root / "baseline.json"
    _write_baseline(
        baseline,
        [
            {
                "paths": ["tests/test_a.py", "tests/test_b.py"],
                "sha256": digest,
            }
        ],
    )
    before = baseline.read_bytes()

    result = _run(root, baseline, "--update-baseline")

    assert result.returncode == 1
    assert "added tracked path(s)" in result.stderr
    assert "refusing to update exact-duplicate baseline with new debt" in result.stderr
    assert baseline.read_bytes() == before


def test_update_refuses_growth_without_writing(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    content = b"def test_value():\n    assert 1 == 1\n"
    _write_bytes(root, "test_a.py", content)
    _write_bytes(root, "test_b.py", content)
    _track_tests(root)
    baseline = root / "baseline.json"
    _write_baseline(baseline, [])
    before = baseline.read_bytes()

    result = _run(root, baseline, "--update-baseline")

    assert result.returncode == 1
    assert "refusing to update exact-duplicate baseline with new debt" in result.stderr
    assert baseline.read_bytes() == before


def test_reduction_requires_baseline_tightening(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    content = b"def test_value():\n    assert 1 == 1\n"
    digest = hashlib.sha256(content).hexdigest()
    _write_bytes(root, "test_a.py", content)
    _track_tests(root)
    baseline = root / "baseline.json"
    _write_baseline(
        baseline,
        [
            {
                "paths": ["tests/test_a.py", "tests/test_b.py"],
                "sha256": digest,
            }
        ],
    )

    stale = _run(root, baseline)
    updated = _run(root, baseline, "--update-baseline")

    assert stale.returncode == 1
    assert f"exact duplicate group {digest} reduced:" in stale.stderr
    assert (
        "uv run --frozen python scripts/check_exact_test_duplicates.py --update-baseline"
    ) in stale.stderr
    assert updated.returncode == 0
    assert (
        "[+] updated exact-duplicate baseline: removed 1 resolved group(s) and 2 stale path entries"
    ) in updated.stdout
    assert json.loads(baseline.read_text(encoding="utf-8")) == _payload([])


def test_malformed_digest_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    baseline = root / "baseline.json"
    _write_baseline(
        baseline,
        [
            {
                "paths": ["tests/test_a.py", "tests/test_b.py"],
                "sha256": ("c7f0b529522a2a44e5436097426a694e361ff3c42f03fb5d7ccdef6a7792"),
            }
        ],
    )

    result = _run(root, baseline)

    assert result.returncode == 2
    assert result.stdout == ""
    assert "[x]" in result.stderr


def test_tracked_symlink_inside_root_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    target = _write_bytes(
        root,
        "fixture.py",
        b"def test_value():\n    assert 1 == 1\n",
    )
    link = root / "tests" / "test_link.py"
    try:
        link.symlink_to(target.name)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")
    _track_tests(root)
    baseline = root / "baseline.json"
    _write_baseline(baseline, [])

    result = _run(root, baseline)

    assert result.returncode == 2
    assert "tracked Python path is a symlink: tests/test_link.py" in result.stderr


def test_symlink_outside_root_fails_containment_check(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    tests = root / "tests"
    tests.mkdir(parents=True)
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"def test_value():\n    assert 1 == 1\n")
    link = tests / "test_escape.py"
    try:
        link.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")
    _track_tests(root)
    baseline = root / "baseline.json"
    _write_baseline(baseline, [])

    result = _run(root, baseline)

    assert result.returncode == 2
    assert "tracked Python path resolves outside repository" in result.stderr


def test_worktree_edits_are_hashed_without_staging(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    first = b"def test_value():\n    assert 1 == 1\n"
    _write_bytes(root, "test_a.py", first)
    second = _write_bytes(root, "test_b.py", b"def test_value():\n    assert 2 == 2\n")
    _track_tests(root)
    second.write_bytes(first)
    baseline = root / "baseline.json"
    _write_baseline(baseline, [])

    result = _run(root, baseline)

    assert result.returncode == 1
    assert "new exact duplicate group" in result.stderr


def test_untracked_duplicate_is_out_of_scope(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    content = b"def test_value():\n    assert 1 == 1\n"
    _write_bytes(root, "test_tracked.py", content)
    _track_tests(root)
    _write_bytes(root, "test_untracked.py", content)
    baseline = root / "baseline.json"
    _write_baseline(baseline, [])

    result = _run(root, baseline)

    assert result.returncode == 0
    assert "1 files, 0 allowed duplicate group(s)" in result.stdout


def test_provisional_baseline_requires_explicit_allow(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_bytes(root, "test_tracked.py", b"def test_value():\n    assert 1 == 1\n")
    _track_tests(root)
    baseline = root / "baseline.json"
    payload = _payload([])
    payload["provisional"] = {
        "basis_commit": "abc123",
        "required_follow_up": "remeasure",
    }
    baseline.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    final = _run(root, baseline)
    provisional = _run(root, baseline, "--allow-provisional")
    payload = json.loads(baseline.read_text(encoding="utf-8"))
    del payload["provisional"]
    baseline.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    finalized = _run(root, baseline)

    assert final.returncode == 2
    assert "provisional baseline is not allowed in final mode" in final.stderr
    assert provisional.returncode == 0
    assert provisional.stderr == ""
    assert finalized.returncode == 0
    assert finalized.stderr == ""
