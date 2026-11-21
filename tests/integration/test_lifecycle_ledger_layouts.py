"""A committed lifecycle bug ledger reads in either layout, and row additions merge."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from apm_cli.utils.git_env import get_git_executable, git_subprocess_env
from scripts.lifecycle_ledger import (
    LEDGER_DIR,
    LEGACY_LEDGER,
    read_ledger,
    read_ledger_at,
    write_ledger,
)

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-native-lifecycle-evidence")]


def _ledger(*issues: int) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "scope": "fixture",
        "property_catalog": [{"id": "idempotency.byte_stable", "phase": 1}],
        "bugs": [
            {"issue": issue, "summary": f"bug {issue}", "properties": ["idempotency.byte_stable"]}
            for issue in issues
        ],
        "lifecycle_contracts": [],
        "known_gaps": [{"id": "gap-a", "properties": []}],
    }


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        [get_git_executable(), *args],
        cwd=repo,
        env=git_subprocess_env(),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    _git(repo, "config", "user.name", "Fixture")
    return repo


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def test_committed_ledger_reads_in_either_layout(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    legacy_path = repo / LEGACY_LEDGER
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(json.dumps(_ledger(1, 2), indent=2) + "\n", encoding="ascii")
    legacy = _commit(repo, "legacy layout")
    legacy_text = legacy_path.read_text(encoding="ascii")
    legacy_path.unlink()
    write_ledger(repo, _ledger(1, 2, 3))
    split = _commit(repo, "per-row layout")
    _git(repo, "rm", "-q", "-r", LEDGER_DIR)

    assert read_ledger_at(repo, legacy) == json.loads(legacy_text)
    assert read_ledger_at(repo, split) == _ledger(1, 2, 3)
    assert not (repo / LEDGER_DIR).exists()


@pytest.mark.parametrize("first", ["left", "right"])
def test_concurrent_row_additions_merge_in_either_order(tmp_path: Path, first: str) -> None:
    repo = _repo(tmp_path)
    write_ledger(repo, _ledger(1))
    _commit(repo, "base")
    for branch, issue in (("left", 2), ("right", 3)):
        _git(repo, "checkout", "-q", "-b", branch, "main")
        write_ledger(repo, _ledger(1, issue))
        _commit(repo, f"add {issue}")

    second = "right" if first == "left" else "left"
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-edit", first)
    _git(repo, "merge", "-q", "--no-edit", second)

    assert sorted(path.name for path in (repo / LEDGER_DIR / "bugs").iterdir()) == [
        "1.json",
        "2.json",
        "3.json",
    ]
    assert read_ledger(repo) == _ledger(1, 2, 3)
