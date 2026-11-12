"""Fixture contracts for the Stage 2 complexity ratchet (microsoft/apm#1076)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

RATCHET_TEST_SCOPE = "fixture"

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check_complexity_ratchet.py"
BASELINE_RELATIVE = "tests/quality/complexity_baseline.json"
SCOPE = [
    "scripts/architecture_linter",
    "scripts/lint_architecture_boundaries.py",
    "scripts/windows_native_symlink_probe_entry.py",
    "src",
    "tests",
]
LIMITS = {
    "max-args": 12,
    "max-branches": 40,
    "max-complexity": 35,
    "max-returns": 8,
    "max-statements": 120,
}
RULES = ["C901", "PLR0911", "PLR0912", "PLR0913", "PLR0915"]

MANY_ARGS = "a, b, c, d, e, f, g, h, i, j, k, l, m"
CLEAN_MODULE = "def ok(value):\n    return value\n"
VIOLATING_MODULE = (
    "class Service:\n"
    f"    def configure(self, {MANY_ARGS}):\n"
    f"        def nested({MANY_ARGS}):\n"
    "            return a\n"
    "        return nested\n"
)


def _payload(violations: dict[str, dict[str, list[str]]]) -> dict[str, object]:
    return {
        "limits": LIMITS,
        "rules": RULES,
        "schema_version": 1,
        "scope": SCOPE,
        "violations": violations,
    }


def _scaffold(root: Path) -> None:
    for directory in ("scripts/architecture_linter", "src", "tests"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    for name in (
        "scripts/lint_architecture_boundaries.py",
        "scripts/windows_native_symlink_probe_entry.py",
    ):
        (root / name).write_text(CLEAN_MODULE, encoding="utf-8")


def _write_module(root: Path, content: str) -> None:
    (root / "src" / "service.py").write_text(content, encoding="utf-8")


def _write_baseline(root: Path, violations: dict[str, dict[str, list[str]]]) -> Path:
    path = root / BASELINE_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_payload(violations), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _commit_all(root: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

    if not (root / ".git").exists():
        git("init", "-q")
        git("config", "user.email", "ratchet@example.invalid")
        git("config", "user.name", "Ratchet Test")
        git("config", "commit.gpgsign", "false")
    git("add", "-A")
    git("commit", "-q", "-m", "snapshot")


def _run(root: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), *extra],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


GRANDFATHERED = {
    "src/service.py": {
        "Service.configure": ["PLR0913"],
        "Service.configure.<locals>.nested": ["PLR0913"],
    }
}


def test_new_violation_outside_baseline_fails(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    _write_module(tmp_path, VIOLATING_MODULE)
    _write_baseline(tmp_path, {})

    result = _run(tmp_path)

    assert result.returncode == 1
    assert "new complexity violation: src/service.py::Service.configure PLR0913" in (
        result.stderr
    )
    assert "src/service.py::Service.configure.<locals>.nested PLR0913" in result.stderr


def test_update_refuses_to_add_new_violation(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    _write_module(tmp_path, VIOLATING_MODULE)
    baseline = _write_baseline(tmp_path, {})
    before = baseline.read_bytes()

    result = _run(tmp_path, "--update-baseline")

    assert result.returncode == 1
    assert "refusing to update complexity baseline with new violations" in result.stderr
    assert baseline.read_bytes() == before


def test_grandfathered_violations_pass_without_line_numbers(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    _write_module(tmp_path, VIOLATING_MODULE)
    _write_baseline(tmp_path, GRANDFATHERED)

    result = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "[+] complexity ratchet clean: 2 grandfathered violation(s)" in result.stdout

    # Moving the grandfathered function to another line keeps the same key.
    _write_module(tmp_path, "\n\n\n" + VIOLATING_MODULE)
    assert _run(tmp_path).returncode == 0


def test_baseline_growth_against_base_ref_fails(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    _write_module(tmp_path, CLEAN_MODULE)
    _write_baseline(tmp_path, {})
    _commit_all(tmp_path)

    # The PR adds a violator AND grandfathers it in the same change.
    _write_module(tmp_path, VIOLATING_MODULE)
    _write_baseline(tmp_path, GRANDFATHERED)

    result = _run(tmp_path, "--base-ref", "HEAD")

    assert result.returncode == 1
    assert "complexity baseline grew against HEAD" in result.stderr
    assert "src/service.py::Service.configure PLR0913" in result.stderr


def test_added_rule_on_grandfathered_function_counts_as_growth(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    _write_module(tmp_path, VIOLATING_MODULE)
    _write_baseline(tmp_path, GRANDFATHERED)
    _commit_all(tmp_path)

    widened = {
        "src/service.py": {
            **GRANDFATHERED["src/service.py"],
            "Service.configure": ["PLR0911", "PLR0913"],
        }
    }
    _write_baseline(tmp_path, widened)

    result = _run(tmp_path, "--base-ref", "HEAD")

    assert result.returncode == 1
    assert "complexity baseline grew against HEAD" in result.stderr
    assert "src/service.py::Service.configure PLR0911" in result.stderr


def test_shrink_passes_against_base_ref(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    _write_module(tmp_path, VIOLATING_MODULE)
    _write_baseline(tmp_path, GRANDFATHERED)
    _commit_all(tmp_path)

    # The PR fixes the violators and removes their baseline entries.
    _write_module(tmp_path, CLEAN_MODULE)
    _write_baseline(tmp_path, {})

    result = _run(tmp_path, "--base-ref", "HEAD")

    assert result.returncode == 0, result.stderr
    assert "[+] complexity ratchet clean: 0 grandfathered violation(s)" in result.stdout


def test_fixed_function_must_leave_baseline(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    _write_module(tmp_path, CLEAN_MODULE)
    baseline = _write_baseline(tmp_path, GRANDFATHERED)

    stale = _run(tmp_path)

    assert stale.returncode == 1
    assert "complexity baseline is stale" in stale.stderr
    assert "src/service.py::Service.configure PLR0913" in stale.stderr

    pruned = _run(tmp_path, "--update-baseline")

    assert pruned.returncode == 0, pruned.stderr
    assert "removed 2 resolved violation(s)" in pruned.stdout
    assert json.loads(baseline.read_text(encoding="utf-8")) == _payload({})
    assert _run(tmp_path).returncode == 0


def test_noqa_does_not_bypass_the_ratchet(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    _write_module(
        tmp_path,
        f"def configure({MANY_ARGS}):  # noqa: PLR0913\n    return a\n",
    )
    _write_baseline(tmp_path, {})

    result = _run(tmp_path)

    assert result.returncode == 1
    assert "src/service.py::configure PLR0913" in result.stderr


def test_bootstrap_without_base_baseline_skips_growth_check(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    _write_module(tmp_path, VIOLATING_MODULE)
    _commit_all(tmp_path)
    _write_baseline(tmp_path, GRANDFATHERED)

    result = _run(tmp_path, "--base-ref", "HEAD")

    assert result.returncode == 0, result.stderr
    assert "no complexity baseline at HEAD" in result.stdout


def test_loosened_limits_are_rejected(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    _write_module(tmp_path, CLEAN_MODULE)
    baseline = _write_baseline(tmp_path, {})
    payload = json.loads(baseline.read_text(encoding="utf-8"))
    payload["limits"]["max-args"] = 20
    baseline.write_text(json.dumps(payload), encoding="utf-8")

    result = _run(tmp_path)

    assert result.returncode == 2
    assert "limits must remain exact" in result.stderr
