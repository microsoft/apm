"""TDD unit tests for the canonical Windows stable-path owner checker.

These tests build small, hermetic fake repository trees under
`tmp_path` rather than touching the real repository, so they exercise
the checker's logic (owner presence, duplicate detection shapes,
exemption, exclusion, nested discovery, CLI behavior) in isolation.
The real tree is separately asserted clean by
`tests/integration/test_architecture_authorities.py`.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[3] / "scripts" / "check_windows_stable_path_owner.py"
_MODULE_NAME = "check_windows_stable_path_owner"

VALID_INSTALL_PS1 = """\
# install.ps1 (fake, minimal)
function Add-ToUserPath {
    param([string]$PathEntry)
}

$installRoot = "C:\\Users\\example\\.apm"
$currentDir = Join-Path $installRoot "current"
$currentExe = Join-Path $currentDir "apm.exe"
Add-ToUserPath -PathEntry $currentDir
"""


def _load_checker() -> ModuleType:
    """Import the checker script as a module without sys.path tricks."""
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclasses (which look up the defining
    # module in sys.modules) can resolve it during class creation.
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


def _make_valid_repo(root: Path) -> None:
    """Populate `root` with a minimal, checker-clean fake repository."""
    (root / "install.ps1").write_text(VALID_INSTALL_PS1, encoding="utf-8")
    (root / "src" / "apm_cli").mkdir(parents=True, exist_ok=True)
    (root / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "windows").mkdir(parents=True, exist_ok=True)


def test_owner_statements_present_reports_no_violations(tmp_path: Path) -> None:
    """A well-formed install.ps1 must not raise owner violations."""
    _make_valid_repo(tmp_path)

    assert checker.find_owner_violations(tmp_path) == []


def test_owner_statement_missing_is_reported(tmp_path: Path) -> None:
    """Dropping a required owner statement must produce one diagnostic."""
    _make_valid_repo(tmp_path)
    (tmp_path / "install.ps1").write_text(
        VALID_INSTALL_PS1.replace("Add-ToUserPath -PathEntry $currentDir\n", ""),
        encoding="utf-8",
    )

    violations = checker.find_owner_violations(tmp_path)

    assert len(violations) == 1
    assert "Add-ToUserPath -PathEntry $currentDir" in violations[0]


def test_missing_install_ps1_is_reported(tmp_path: Path) -> None:
    """A repository with no install.ps1 at all must fail loudly."""
    (tmp_path / "src" / "apm_cli").mkdir(parents=True, exist_ok=True)

    violations = checker.find_owner_violations(tmp_path)

    assert len(violations) == 1
    assert "install.ps1" in violations[0]


def test_positional_join_path_duplicate_is_detected(tmp_path: Path) -> None:
    """Join-Path $x "current" (positional form) is a duplicate derivation."""
    _make_valid_repo(tmp_path)
    culprit = tmp_path / "src" / "apm_cli" / "rogue.py"
    culprit.write_text('bad = Join-Path $installRoot "current"\n', encoding="utf-8")

    hits = checker.find_duplicate_hits(tmp_path)

    assert len(hits) == 1
    assert hits[0].path == culprit
    assert hits[0].line_no == 1


def test_named_parameter_join_path_duplicate_is_detected(tmp_path: Path) -> None:
    """Join-Path -Path $x -ChildPath "current" (named form) is a duplicate."""
    _make_valid_repo(tmp_path)
    culprit = tmp_path / "scripts" / "windows" / "rogue.ps1"
    culprit.write_text(
        '$dup = Join-Path -Path $installRoot -ChildPath "current"\n',
        encoding="utf-8",
    )

    hits = checker.find_duplicate_hits(tmp_path)

    assert len(hits) == 1
    assert hits[0].path == culprit


def test_named_parameter_join_path_duplicate_reordered_is_detected(tmp_path: Path) -> None:
    """The -ChildPath / -Path named arguments may appear in either order."""
    _make_valid_repo(tmp_path)
    culprit = tmp_path / "scripts" / "windows" / "rogue.ps1"
    culprit.write_text(
        '$dup = Join-Path -ChildPath "current" -Path $installRoot\n',
        encoding="utf-8",
    )

    hits = checker.find_duplicate_hits(tmp_path)

    assert len(hits) == 1
    assert hits[0].path == culprit


@pytest.mark.parametrize("separator", ["\\", "/"])
def test_literal_stable_path_duplicate_is_detected(tmp_path: Path, separator: str) -> None:
    """A literal current\\apm.exe or current/apm.exe path is a duplicate."""
    _make_valid_repo(tmp_path)
    culprit = tmp_path / "src" / "apm_cli" / "rogue.py"
    culprit.write_text(f'exe = "current{separator}apm.exe"\n', encoding="utf-8")

    hits = checker.find_duplicate_hits(tmp_path)

    assert len(hits) == 1
    assert hits[0].path == culprit


@pytest.mark.parametrize("separator", ["\\", "/"])
def test_similarly_named_identifier_is_not_a_false_positive(tmp_path: Path, separator: str) -> None:
    """ "concurrent/apm.exe" must not be mistaken for the stable "current" path.

    The literal-path branch matched "current[\\/]apm.exe" anywhere in the
    line, so a substring like "concurrent/apm.exe" (or the Windows-
    separator form) tripped a false positive purely because "concurrent"
    contains "current" as a substring. The check must require "current"
    to start at a word boundary so unrelated identifiers ending in
    "current" are left alone, while both real path separators for an
    actual standalone "current" segment keep matching (see the
    parametrized ``test_literal_stable_path_duplicate_is_detected`` above).
    """
    _make_valid_repo(tmp_path)
    innocent = tmp_path / "src" / "apm_cli" / "innocent.py"
    innocent.write_text(f'path = "concurrent{separator}apm.exe"\n', encoding="utf-8")

    assert checker.find_duplicate_hits(tmp_path) == []


def test_exemption_marker_suppresses_a_duplicate_line(tmp_path: Path) -> None:
    """A line-level architecture-authority-exempt marker is honored."""
    _make_valid_repo(tmp_path)
    exempted = tmp_path / "src" / "apm_cli" / "rogue.py"
    exempted.write_text(
        'exe = "current/apm.exe"  # architecture-authority-exempt: demo\n',
        encoding="utf-8",
    )

    assert checker.find_duplicate_hits(tmp_path) == []


def test_test_prefixed_windows_script_is_excluded(tmp_path: Path) -> None:
    """Black-box validators named test-*.ps1 are not scanned as owners."""
    _make_valid_repo(tmp_path)
    validator = tmp_path / "scripts" / "windows" / "test-install-script.ps1"
    validator.write_text('$dup = Join-Path $prefix.Root "current"\n', encoding="utf-8")

    assert checker.find_duplicate_hits(tmp_path) == []


def test_nested_workflow_file_is_discovered(tmp_path: Path) -> None:
    """Workflow files nested under subdirectories must still be scanned."""
    _make_valid_repo(tmp_path)
    nested = tmp_path / ".github" / "workflows" / "shared" / "nested.yml"
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_text('run: echo "current/apm.exe"\n', encoding="utf-8")

    hits = checker.find_duplicate_hits(tmp_path)

    assert len(hits) == 1
    assert hits[0].path == nested


def test_yaml_extension_workflow_file_is_discovered(tmp_path: Path) -> None:
    """Both .yml and .yaml workflow extensions must be scanned."""
    _make_valid_repo(tmp_path)
    workflow = tmp_path / ".github" / "workflows" / "rogue.yaml"
    workflow.write_text('run: echo "current/apm.exe"\n', encoding="utf-8")

    hits = checker.find_duplicate_hits(tmp_path)

    assert len(hits) == 1
    assert hits[0].path == workflow


def test_check_is_clean_on_a_well_formed_tree(tmp_path: Path) -> None:
    """The combined check() must report nothing for a clean fake tree."""
    _make_valid_repo(tmp_path)

    assert checker.check(tmp_path) == []


def test_cli_exits_nonzero_and_prints_diagnostic_on_violation(tmp_path: Path) -> None:
    """The CLI entry point must fail loudly with an actionable message."""
    _make_valid_repo(tmp_path)
    culprit = tmp_path / "src" / "apm_cli" / "rogue.py"
    culprit.write_text('exe = "current/apm.exe"\n', encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "current/apm.exe" in result.stdout
    assert "rogue.py" in result.stdout


def test_cli_exits_zero_and_prints_success_on_clean_tree(tmp_path: Path) -> None:
    """The CLI entry point must succeed silently-ish on a clean tree."""
    _make_valid_repo(tmp_path)

    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "clean" in result.stdout
