from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from scripts.test_file_inventory import is_test_module_path
from tests.quality.repository_python_inventory import PythonModuleFacts

RATCHET_TEST_SCOPE = "repository"

REPO_ROOT = Path(__file__).resolve().parents[2]
QUALITY_DIR = REPO_ROOT / "tests" / "quality"
CHECKERS = (
    (
        REPO_ROOT / "scripts" / "check_test_assertions.py",
        QUALITY_DIR / "assertion_quality_baseline.json",
        "[+] assertion-quality ratchet clean: AQ001=4, AQ002=12\n",
    ),
    (
        REPO_ROOT / "scripts" / "check_exact_test_duplicates.py",
        QUALITY_DIR / "exact_test_duplicates.json",
        (
            "[+] exact test duplicate ratchet clean: "
            "{module_count} files, 0 allowed duplicate group(s)\n"
        ),
    ),
)
ALLOW_PROVISIONAL_ENV = "APM_ALLOW_PROVISIONAL_BASELINES"


def _assert_provisional_mode(
    *,
    provisional: bool,
    env_value: str | None,
) -> None:
    assert env_value in {None, "0", "1"}
    if provisional:
        assert env_value == "1"


def test_repository_quality_baselines_are_scanned_once(
    monkeypatch: pytest.MonkeyPatch,
    repository_python_inventory: dict[str, PythonModuleFacts],
) -> None:
    invocations: Counter[Path] = Counter()
    real_run = subprocess.run
    module_count = sum(
        path.startswith("tests/") and is_test_module_path(path)
        for path in repository_python_inventory
    )

    def tracked_run(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        script = Path(command[1])
        if script in {entry[0] for entry in CHECKERS}:
            invocations[script] += 1
        return real_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", tracked_run)
    for script, baseline, expected in CHECKERS:
        payload = json.loads(baseline.read_text(encoding="utf-8"))
        command = [sys.executable, str(script)]
        if "provisional" in payload:
            assert os.environ.get(ALLOW_PROVISIONAL_ENV) == "1"
            command.append("--allow-provisional")

        result = subprocess.run(
            command,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout == expected.format(module_count=module_count)
        assert result.stderr == ""
        assert baseline.is_file()

    assert invocations == Counter({script: 1 for script, _baseline, _expected in CHECKERS})


def test_repository_baseline_finalization_state_is_consistent() -> None:
    states = {
        baseline: "provisional" in json.loads(baseline.read_text(encoding="utf-8"))
        for _script, baseline, _expected in CHECKERS
    }
    assert len(set(states.values())) == 1, (
        f"repository quality baselines have mixed finalization state: {states}"
    )
    provisional = next(iter(states.values()))
    _assert_provisional_mode(
        provisional=provisional,
        env_value=os.environ.get(ALLOW_PROVISIONAL_ENV),
    )


def test_duplicate_checker_reports_new_tracked_module_count(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    tests = root / "tests"
    tests.mkdir(parents=True)
    baseline = root / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "algorithm": "sha256-bytes-v1",
                "duplicate_groups": [],
                "schema_version": 1,
                "scope": [
                    "tests/**/test_*.py",
                    "tests/**/*_test.py",
                ],
            }
        ),
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(root)], check=True)

    first = tests / "test_first.py"
    first.write_text("def test_first():\n    assert 1 == 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "tests"], check=True)
    first_result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "check_exact_test_duplicates.py"),
            "--root",
            str(root),
            "--baseline",
            str(baseline),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    second = tests / "second_test.py"
    second.write_text("def test_second():\n    assert 2 == 2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "tests"], check=True)
    second_result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "check_exact_test_duplicates.py"),
            "--root",
            str(root),
            "--baseline",
            str(baseline),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert first_result.returncode == 0
    assert "1 files, 0 allowed duplicate group(s)" in first_result.stdout
    assert second_result.returncode == 0
    assert "2 files, 0 allowed duplicate group(s)" in second_result.stdout


@pytest.mark.parametrize(
    ("provisional", "env_value", "passes"),
    [
        (True, "0", False),
        (True, "1", True),
        (False, "1", True),
        (False, "0", True),
    ],
)
def test_provisional_mode_implication(
    provisional: bool,
    env_value: str,
    passes: bool,
) -> None:
    if passes:
        _assert_provisional_mode(
            provisional=provisional,
            env_value=env_value,
        )
        return

    with pytest.raises(AssertionError):
        _assert_provisional_mode(
            provisional=provisional,
            env_value=env_value,
        )
