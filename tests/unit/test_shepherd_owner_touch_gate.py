"""Deterministic scenarios for shepherd-driver's canonical owner gate."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from apm_cli.utils.git_env import get_git_executable, git_subprocess_env

# Entire module: every scenario here drives the gate through the fixed
# _run_git()/_git_executable() path (microsoft/apm#2233's WinError 2
# FileNotFoundError), since the fixture helper below and the gate
# script itself both resolve git via the canonical apm_cli.utils.git_env
# owner. Selected by the PR-time Windows Compatibility Gate via
# `pytest -m windows_compat`; also runs on every other OS.
pytestmark = pytest.mark.windows_compat

ROOT = Path(__file__).parents[2]
GATE = ROOT / "packages/shepherd-driver/scripts/owner_touch_gate.py"
OWNER_TABLE = ".apm/instructions/architecture.instructions.md"
DECISION = "Fixture durable fact"
OWNER_PATH = "src/apm_cli/fixture_owner.py"


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a fixed, non-interactive test command."""
    return subprocess.run(
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )


def _git(repo: Path, *args: str) -> str:
    """Run git in a fixture repository and return stdout.

    Resolves git's full executable path and uses a sanitized subprocess
    environment via the canonical ``apm_cli.utils.git_env`` owner --
    a bare ``["git", ...]`` argv does not reliably resolve on Windows
    (``CreateProcess`` requires an extension-qualified executable or a
    PATH lookup that ``shell=False`` does not perform), which previously
    raised ``FileNotFoundError: [WinError 2]`` (see microsoft/apm#2233).
    """
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


def _table(
    *,
    header: str = "Owner path selectors",
    selector: str = OWNER_PATH,
    delimiter: str = "---",
) -> str:
    """Return the canonical marked table used by fixture commits."""
    return (
        "# Architecture\n\n"
        "<!-- canonical-owner-table:v1 -->\n"
        f"| Decision / fact | Canonical owner | {header} |\n"
        f"|{delimiter}|{delimiter}|{delimiter}|\n"
        f"| {DECISION} | `{OWNER_PATH}` | `{selector}` |\n"
        "<!-- /canonical-owner-table -->\n"
    )


def _write(repo: Path, relative_path: str, content: str) -> None:
    """Write a fixture file, creating its parent directories."""
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="ascii")


def _commit(repo: Path, message: str) -> str:
    """Commit all fixture changes and return the exact commit SHA."""
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def owner_repo(tmp_path: Path) -> tuple[Path, str]:
    """Create a git repository whose base contains one canonical owner."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    _git(repo, "config", "user.name", "Fixture")
    _write(repo, OWNER_TABLE, _table())
    _write(repo, OWNER_PATH, 'FACT = "base"\n')
    return repo, _commit(repo, "base")


def _gate(
    repo: Path,
    command: str,
    base: str,
    head: str,
    *,
    completion: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke the owner gate exactly as the shepherd primitive does."""
    args = [
        sys.executable,
        str(GATE),
        command,
        "--repo-root",
        str(repo),
        "--base",
        base,
        "--head",
        head,
    ]
    if completion is not None:
        args.extend(["--completion", str(completion)])
    return _run(args, cwd=repo)


def _detect(repo: Path, base: str, head: str) -> dict[str, Any]:
    """Run detection and decode its JSON result."""
    result = _gate(repo, "detect", base, head)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _completion(
    report: dict[str, Any],
    *,
    classification: str,
    include_test: bool,
) -> dict[str, Any]:
    """Build terminal evidence for semantic gate scenarios."""
    tests = []
    if include_test:
        tests.append(
            {
                "test_id": "tests/fixture.py::test_fact",
                "command": "pytest tests/fixture.py::test_fact -q",
                "outcome": "passed",
                "head_sha": report["head_sha"],
                "owner_decisions": [DECISION],
                "run_evidence": "1 passed in 0.01s",
            }
        )
    return {
        "status": "ready-to-merge",
        "architecture_evidence": {
            "version": "2",
            "classification": classification,
            "owner_touch_report": report,
            "functional_tests": tests,
        },
    }


def _verify(
    repo: Path,
    base: str,
    head: str,
    completion: dict[str, Any],
) -> subprocess.CompletedProcess[str]:
    """Persist and semantically verify one completion fixture."""
    completion_path = repo / "completion.json"
    completion_path.write_text(json.dumps(completion), encoding="ascii")
    return _gate(repo, "verify", base, head, completion=completion_path)


def test_positive_owner_touch_is_deterministic_and_verifies(
    owner_repo: tuple[Path, str],
) -> None:
    """A touched owner with passing exact-head evidence verifies."""
    repo, base = owner_repo
    _write(repo, OWNER_PATH, 'FACT = "changed"\n')
    head = _commit(repo, "touch owner")

    first = _detect(repo, base, head)
    second = _detect(repo, base, head)
    result = _verify(
        repo,
        base,
        head,
        _completion(first, classification="owner-extension", include_test=True),
    )

    assert first == second
    assert first["touched_owners"][0]["decision"] == DECISION
    assert first["touched_owners"][0]["matched_files"] == [OWNER_PATH]
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["functional_test_ids"] == ["tests/fixture.py::test_fact"]


def test_missing_functional_evidence_fails_closed(
    owner_repo: tuple[Path, str],
) -> None:
    """A touched owner without an executed test cannot verify."""
    repo, base = owner_repo
    _write(repo, OWNER_PATH, 'FACT = "changed"\n')
    head = _commit(repo, "touch owner")
    report = _detect(repo, base, head)

    result = _verify(
        repo,
        base,
        head,
        _completion(report, classification="owner-extension", include_test=False),
    )

    assert result.returncode == 1
    assert "missing executed functional evidence" in result.stderr


def test_false_self_classification_fails_closed(
    owner_repo: tuple[Path, str],
) -> None:
    """An LLM cannot label a detected owner touch ordinary."""
    repo, base = owner_repo
    _write(repo, OWNER_PATH, 'FACT = "changed"\n')
    head = _commit(repo, "touch owner")
    report = _detect(repo, base, head)

    result = _verify(
        repo,
        base,
        head,
        _completion(report, classification="ordinary-fix", include_test=True),
    )

    assert result.returncode == 1
    assert "classification self-exempts" in result.stderr


def test_unrelated_diff_has_no_owner_touch_and_needs_no_test(
    owner_repo: tuple[Path, str],
) -> None:
    """An unrelated primitive diff does not create a functional-test burden."""
    repo, base = owner_repo
    _write(repo, ".apm/skills/fixture/SKILL.md", "# Fixture\n")
    head = _commit(repo, "unrelated primitive")
    report = _detect(repo, base, head)

    result = _verify(
        repo,
        base,
        head,
        _completion(report, classification="not-applicable", include_test=False),
    )

    assert report["touched_owners"] == []
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["touched_owner_count"] == 0


def test_owner_table_header_drift_fails_closed(
    owner_repo: tuple[Path, str],
) -> None:
    """A changed owner-table contract cannot silently disable detection."""
    repo, base = owner_repo
    _write(repo, OWNER_TABLE, _table(header="Selectors"))
    head = _commit(repo, "drift owner table")

    result = _gate(repo, "detect", base, head)

    assert result.returncode == 1
    assert "canonical owner table header drifted" in result.stderr


@pytest.mark.parametrize(
    ("table", "diagnostic"),
    [
        (_table(delimiter=":"), "canonical owner table delimiter drifted"),
        (
            _table(selector=f"{OWNER_PATH};;src/apm_cli/other.py"),
            "canonical owner row has no selectors",
        ),
        (
            _table(selector=f"`{OWNER_PATH}"),
            "malformed owner path selector",
        ),
    ],
)
def test_malformed_owner_table_syntax_fails_closed(
    owner_repo: tuple[Path, str],
    table: str,
    diagnostic: str,
) -> None:
    """Malformed delimiters and selector segments are never normalized."""
    repo, base = owner_repo
    _write(repo, OWNER_TABLE, table)
    head = _commit(repo, "malform owner table")

    result = _gate(repo, "detect", base, head)

    assert result.returncode == 1
    assert diagnostic in result.stderr


def test_owner_table_unmatchable_selector_fails_closed(
    owner_repo: tuple[Path, str],
) -> None:
    """A stale selector cannot silently make its owner row unreachable."""
    repo, base = owner_repo
    _write(repo, OWNER_TABLE, _table(selector="src/apm_cli/missing.py"))
    head = _commit(repo, "drift owner selector")

    result = _gate(repo, "detect", base, head)

    assert result.returncode == 1
    assert "canonical owner selector matches no exact-head file" in result.stderr


def test_stale_owner_table_report_fails_closed(
    owner_repo: tuple[Path, str],
) -> None:
    """A report captured before owner-table drift cannot verify a later head."""
    repo, base = owner_repo
    _write(repo, OWNER_PATH, 'FACT = "changed"\n')
    first_head = _commit(repo, "touch owner")
    stale_report = _detect(repo, base, first_head)
    _write(repo, OWNER_TABLE, _table() + "\n# Clarified owner notes\n")
    current_head = _commit(repo, "clarify owner table")

    result = _verify(
        repo,
        base,
        current_head,
        _completion(
            stale_report,
            classification="owner-extension",
            include_test=True,
        ),
    )

    assert result.returncode == 1
    assert "owner_touch_report does not match fresh exact-head detection" in result.stderr


def test_deleted_owner_under_broad_selector_is_detected(
    owner_repo: tuple[Path, str],
) -> None:
    """Deleted owner paths remain part of deterministic touch detection."""
    repo, _ = owner_repo
    _write(repo, OWNER_TABLE, _table(selector="src/apm_cli/*.py"))
    _write(repo, "src/apm_cli/other.py", 'FACT = "other"\n')
    base = _commit(repo, "broaden owner selector")
    (repo / OWNER_PATH).unlink()
    head = _commit(repo, "delete owner")

    report = _detect(repo, base, head)

    assert report["touched_owners"][0]["matched_files"] == [OWNER_PATH]


def test_type_changed_owner_is_detected(owner_repo: tuple[Path, str]) -> None:
    """Replacing an owner file with a symlink cannot bypass the gate."""
    repo, base = owner_repo
    owner = repo / OWNER_PATH
    owner.unlink()
    owner.symlink_to("fixture_target.py")
    _write(repo, "src/apm_cli/fixture_target.py", 'FACT = "target"\n')
    head = _commit(repo, "replace owner with symlink")

    report = _detect(repo, base, head)

    assert report["touched_owners"][0]["matched_files"] == [OWNER_PATH]


def test_rename_away_keeps_old_owner_endpoint(
    owner_repo: tuple[Path, str],
) -> None:
    """A broad selector detects an owner renamed outside its matched tree."""
    repo, _ = owner_repo
    _write(repo, OWNER_TABLE, _table(selector="src/apm_cli/*.py"))
    _write(repo, "src/apm_cli/other.py", 'FACT = "other"\n')
    base = _commit(repo, "broaden owner selector")
    moved = repo / "archive/fixture_owner.py"
    moved.parent.mkdir()
    (repo / OWNER_PATH).rename(moved)
    head = _commit(repo, "rename owner away")

    report = _detect(repo, base, head)

    assert report["touched_owners"][0]["matched_files"] == [OWNER_PATH]


def test_removed_owner_row_still_detects_base_selector(
    owner_repo: tuple[Path, str],
) -> None:
    """Removing an owner row and its file cannot erase the prior authority."""
    repo, _ = owner_repo
    second_row = "| Other fact | `src/apm_cli/other.py` | `src/apm_cli/other.py` |\n"
    table = _table().replace(
        "<!-- /canonical-owner-table -->",
        second_row + "<!-- /canonical-owner-table -->",
    )
    _write(repo, OWNER_TABLE, table)
    _write(repo, "src/apm_cli/other.py", 'FACT = "other"\n')
    base = _commit(repo, "add second owner")
    table_without_owner = table.replace(
        f"| {DECISION} | `{OWNER_PATH}` | `{OWNER_PATH}` |\n",
        "",
    )
    _write(repo, OWNER_TABLE, table_without_owner)
    (repo / OWNER_PATH).unlink()
    head = _commit(repo, "remove owner row and file")

    report = _detect(repo, base, head)

    assert report["touched_owners"][0]["decision"] == DECISION
    assert report["touched_owners"][0]["matched_files"] == [OWNER_PATH]


def test_unknown_completion_status_fails_closed(
    owner_repo: tuple[Path, str],
) -> None:
    """Only schema-defined non-terminal statuses bypass evidence checks."""
    repo, base = owner_repo
    _write(repo, ".apm/skills/fixture/SKILL.md", "# Fixture\n")
    head = _commit(repo, "unrelated primitive")

    result = _verify(repo, base, head, {"status": "ready"})

    assert result.returncode == 1
    assert "unsupported completion status" in result.stderr


# ---------------------------------------------------------------------------
# Windows bare-argv git resolution regression (microsoft/apm#2233)
#
# subprocess.run(["git", ...]) does not reliably resolve on Windows with
# shell=False (CreateProcess needs an extension-qualified path or a PATH
# search this test's environment previously did not perform), raising
# FileNotFoundError: [WinError 2]. The fix resolves git's full path once
# via shutil.which and reuses it for every invocation. These regression
# tests cover the gate script itself (packages/shepherd-driver/scripts/
# owner_touch_gate.py::_git), not just this test file's own fixture
# helper, since both had the identical bug.
# ---------------------------------------------------------------------------


def _load_gate_module() -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location("owner_touch_gate", GATE)
    assert spec is not None and spec.loader is not None
    module = ModuleType("owner_touch_gate")
    import sys as _sys

    _sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_gate_git_helper_never_uses_bare_argv() -> None:
    """The production gate script must not pass a bare "git" argv to
    subprocess -- it must resolve git's full path first (see
    microsoft/apm#2233)."""
    source = GATE.read_text(encoding="utf-8")
    assert '["git"' not in source
    assert "['git'" not in source


def test_gate_git_executable_is_cached_and_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    """The resolved git path is looked up via shutil.which and cached."""
    module = _load_gate_module()
    calls = {"count": 0}

    def fake_which(name: str) -> str:
        assert name == "git"
        calls["count"] += 1
        return "/usr/bin/git"

    monkeypatch.setattr(module.shutil, "which", fake_which)
    module._GIT_EXECUTABLE = None

    first = module._git_executable()
    second = module._git_executable()

    assert first == "/usr/bin/git"
    assert second == "/usr/bin/git"
    assert calls["count"] == 1, "git executable lookup must be cached, not repeated"


def test_gate_git_executable_fails_closed_when_git_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing git binary must raise a clear GateError, not an opaque
    platform-specific FileNotFoundError."""
    module = _load_gate_module()
    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    module._GIT_EXECUTABLE = None

    with pytest.raises(module.GateError, match="git executable not found"):
        module._git_executable()
