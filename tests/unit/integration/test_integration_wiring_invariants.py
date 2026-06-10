"""Structural invariants for the #1166 pytest-discovery migration.

Regression fence: these tests ensure the migration from per-file script
enumeration (``pytest test_X.py`` x20) to directory-wide discovery
(``pytest tests/integration/``) cannot silently revert.

Three invariants are checked:

1. Shell-script discovery - ``scripts/test-integration.sh`` must invoke
   ``pytest tests/integration/`` at least once.  It must NOT contain any
   per-file invocation of the form ``pytest tests/integration/test_*.py``.

2. Marker-registry completeness - every ``requires_*`` marker key present
   in ``_MARKER_CHECKS`` (``tests/integration/conftest.py``) must be
   declared in ``[tool.pytest.ini_options].markers`` in ``pyproject.toml``
   so pytest does not warn about unknown markers.

3. Reverse completeness - every ``requires_*`` marker declared in
   ``pyproject.toml`` must have a corresponding entry in ``_MARKER_CHECKS``
   so its precondition is actually enforced at collection time.

See microsoft/apm#1166 for the design rationale.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_INTEGRATION_SCRIPT = _REPO_ROOT / "scripts" / "test-integration.sh"
_INTEGRATION_CONFTEST = _REPO_ROOT / "tests" / "integration" / "conftest.py"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

# Pattern that would indicate a per-file invocation still lurking in the script.
# Matches lines like: pytest tests/integration/test_foo.py
# Allow the pattern inside comments (lines starting with #) since historical
# examples in comments are fine.
_PER_FILE_PATTERN = re.compile(
    r"^\s*[^#].*pytest\s+tests/integration/test_\w+\.py",
    re.MULTILINE,
)

# Pattern that matches the preferred directory-wide invocation present in the script.
# Matches "pytest tests/integration/" NOT immediately followed by a test filename.
# The [^#] anchor mirrors _PER_FILE_PATTERN to exclude commented-out lines.
_DIRECTORY_INVOCATION = re.compile(
    r"^\s*[^#].*pytest\s+tests/integration/(?!test_\w+\.py)",
    re.MULTILINE,
)

# Marker prefix that indicates a gating marker (vs. informational ones).
_REQUIRES_PREFIX = "requires_"


def _load_conftest_marker_keys() -> set[str]:
    """Return the set of marker names registered in ``_MARKER_CHECKS``."""
    text = _INTEGRATION_CONFTEST.read_text(encoding="utf-8")
    # Extract keys from the dict literal: `"requires_foo": (...)`
    return set(re.findall(r'"(requires_\w+)":\s*\(', text))


def _load_pyproject_markers() -> set[str]:
    """Return the set of marker names declared in pyproject.toml."""
    text = _PYPROJECT.read_text(encoding="utf-8")
    # Each line in the markers list looks like:
    #   "requires_foo: description ...",
    return set(re.findall(r'"(requires_\w+):', text))


# ---------------------------------------------------------------------------
# Test 1: shell script uses directory-wide discovery
# ---------------------------------------------------------------------------


def test_script_no_per_file_pytest_invocations() -> None:
    """``scripts/test-integration.sh`` must not enumerate individual test files.

    Per-file invocations (``pytest tests/integration/test_X.py``) defeat
    auto-discovery and silently leave new test files unexecuted.  The
    script must delegate file selection entirely to pytest.
    """
    if not _INTEGRATION_SCRIPT.is_file():
        pytest.fail(f"Integration script not found: {_INTEGRATION_SCRIPT}")

    text = _INTEGRATION_SCRIPT.read_text(encoding="utf-8")
    matches = _PER_FILE_PATTERN.findall(text)
    assert not matches, (
        "scripts/test-integration.sh contains per-file pytest invocations "
        "(issue #1166 regression). Remove them and use ``pytest tests/integration/`` "
        f"instead. Offending lines: {matches}"
    )


def test_script_uses_directory_invocation() -> None:
    """``scripts/test-integration.sh`` must invoke ``pytest tests/integration/``.

    This confirms the single-directory invocation that replaced the 20+
    per-file blocks introduced in PR #1247 (issue #1166) is still present.
    """
    if not _INTEGRATION_SCRIPT.is_file():
        pytest.fail(f"Integration script not found: {_INTEGRATION_SCRIPT}")

    text = _INTEGRATION_SCRIPT.read_text(encoding="utf-8")
    assert _DIRECTORY_INVOCATION.search(text), (
        "scripts/test-integration.sh does not contain ``pytest tests/integration/``. "
        "The directory-wide invocation must remain as the single test-selection entry point."
    )


# ---------------------------------------------------------------------------
# Test 2: conftest markers declared in pyproject.toml
# ---------------------------------------------------------------------------


def test_conftest_markers_declared_in_pyproject() -> None:
    """Every ``requires_*`` key in ``_MARKER_CHECKS`` must be in pyproject.toml.

    Undeclared markers produce a ``PytestUnknownMarkWarning`` (or error with
    ``--strict-markers``) and hide the skip reason from ``pytest -v`` output.
    """
    conftest_keys = _load_conftest_marker_keys()
    pyproject_keys = _load_pyproject_markers()

    missing = conftest_keys - pyproject_keys
    assert not missing, (
        "The following markers are registered in _MARKER_CHECKS "
        "(tests/integration/conftest.py) but NOT declared in pyproject.toml. "
        "Add them to [tool.pytest.ini_options].markers: " + ", ".join(sorted(missing))
    )


# ---------------------------------------------------------------------------
# Test 3: pyproject.toml markers backed by conftest checks
# ---------------------------------------------------------------------------


def test_pyproject_requires_markers_have_conftest_checks() -> None:
    """Every ``requires_*`` marker declared in pyproject.toml must be enforced.

    A marker listed in pyproject.toml but absent from ``_MARKER_CHECKS`` is
    silently ignored at collection time: tests annotated with it run even when
    the precondition is not satisfied.
    """
    conftest_keys = _load_conftest_marker_keys()
    pyproject_keys = _load_pyproject_markers()

    unenforced = pyproject_keys - conftest_keys
    assert not unenforced, (
        "The following ``requires_*`` markers are declared in pyproject.toml "
        "but have no enforcement entry in _MARKER_CHECKS "
        "(tests/integration/conftest.py). "
        "Add a predicate + skip-reason pair for each: " + ", ".join(sorted(unenforced))
    )
