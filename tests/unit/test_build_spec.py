"""Tests for build spec helper functions (build/apm.spec).

``build/apm.spec`` is a PyInstaller spec file that is executed inside the
PyInstaller runtime.  It cannot be imported normally because the PyInstaller
globals (``SPECPATH``, ``Analysis``, ``PYZ``, ``EXE``, ``COLLECT``) do not
exist outside a build context.

Strategy
--------
1. **Syntax check** -- ``compile()`` the raw spec source.  Catches any Python
   syntax errors introduced by edits.
2. **Function-level extraction** -- use ``ast`` to locate the helper function
   definitions (``is_upx_available``, ``should_use_upx``,
   ``_read_version_from_pyproject``) and ``exec`` only those definitions into a
   controlled namespace.  The rest of the spec (which uses PyInstaller globals)
   is never executed.  This is the same technique used in
   ``tests/unit/test_ssl_cert_hook.py`` for the PyInstaller runtime hook.

This approach keeps the tests hermetic, fast, and dependency-free.
"""

import ast
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_repo_root() -> Path:
    """Walk up from this file until pyproject.toml is found (the repo root)."""
    current = Path(__file__).resolve().parent
    for candidate in [current] + list(current.parents):  # noqa: RUF005
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise RuntimeError("Cannot locate repository root (no pyproject.toml found)")


_REPO_ROOT = _find_repo_root()
_SPEC_FILE = _REPO_ROOT / "build" / "apm.spec"


def _extract_spec_helpers() -> str:
    """Return a self-contained Python source snippet with only the helper
    function definitions extracted from the spec file.

    The returned snippet can be safely ``exec``'d without any PyInstaller
    globals present.  Functions are emitted in source order so that
    ``should_use_upx`` (which calls ``is_upx_available``) is always defined
    after its dependency.
    """
    spec_source = _SPEC_FILE.read_text(encoding="utf-8")
    tree = ast.parse(spec_source)
    lines = spec_source.splitlines()

    wanted = {"is_upx_available", "should_use_upx", "_read_version_from_pyproject"}

    # Standard-library imports the helper functions rely on.
    preamble = [
        "import sys",
        "import subprocess",
        "import os",
        "from pathlib import Path",
    ]

    func_parts: list[str] = []
    for node in tree.body:  # iterate in source order
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            func_src = "\n".join(lines[node.lineno - 1 : node.end_lineno])
            func_parts.append(func_src)

    if not func_parts:
        raise RuntimeError(
            f"No helper functions found in {_SPEC_FILE}. "
            "Check that 'should_use_upx' and '_read_version_from_pyproject' still exist."
        )

    return "\n\n".join(preamble + func_parts)


def _make_helpers_ns(repo_root: Path | None = None) -> dict:
    """Compile and execute the helper functions into a fresh namespace.

    ``repo_root`` is stored in the namespace for convenience  -- callers pass
    it explicitly to ``_read_version_from_pyproject(repo_root)`` at call time.
    """
    ns: dict = {"repo_root": repo_root if repo_root is not None else _REPO_ROOT}
    code = compile(_extract_spec_helpers(), "<spec_helpers>", "exec")
    exec(code, ns)  # noqa: S102
    return ns


# ---------------------------------------------------------------------------
# 1. Syntax check
# ---------------------------------------------------------------------------


class TestSpecFileSyntax:
    """The spec file must be valid Python at all times."""

    def test_spec_file_exists(self):
        assert _SPEC_FILE.is_file(), f"Expected spec file at {_SPEC_FILE}"

    def test_spec_file_compiles_without_syntax_errors(self):
        """``compile()`` the raw source  -- catches SyntaxError without executing
        any PyInstaller-specific globals."""
        source = _SPEC_FILE.read_text(encoding="utf-8")
        try:
            compile(source, str(_SPEC_FILE), "exec")
        except SyntaxError as exc:
            pytest.fail(f"build/apm.spec contains a syntax error: {exc}")

    def test_spec_file_helper_functions_are_extractable(self):
        """AST extraction must succeed and return at least two function defs."""
        snippet = _extract_spec_helpers()
        assert "def should_use_upx" in snippet
        assert "def _read_version_from_pyproject" in snippet
        assert "def is_upx_available" in snippet


# ---------------------------------------------------------------------------
# 2. should_use_upx()
# ---------------------------------------------------------------------------


class TestShouldUseUpx:
    """``should_use_upx()`` disables UPX on Windows, delegates on other platforms."""

    def test_returns_false_on_windows(self, monkeypatch):
        """Regression guard: UPX must be disabled on win32 to avoid AV false positives."""
        monkeypatch.setattr(sys, "platform", "win32")
        ns = _make_helpers_ns()
        result = ns["should_use_upx"]()
        assert result is False, "should_use_upx() must return False on win32"

    def test_delegates_to_is_upx_available_when_upx_present_on_linux(self, monkeypatch):
        """On Linux, should_use_upx() returns True when UPX is installed."""
        monkeypatch.setattr(sys, "platform", "linux")
        ns = _make_helpers_ns()
        ns["is_upx_available"] = lambda: True  # inject mock into helpers' namespace
        result = ns["should_use_upx"]()
        assert result is True

    def test_delegates_to_is_upx_available_when_upx_absent_on_linux(self, monkeypatch):
        """On Linux, should_use_upx() returns False when UPX is not installed."""
        monkeypatch.setattr(sys, "platform", "linux")
        ns = _make_helpers_ns()
        ns["is_upx_available"] = lambda: False
        result = ns["should_use_upx"]()
        assert result is False

    def test_delegates_to_is_upx_available_on_darwin(self, monkeypatch):
        """On macOS (darwin), should_use_upx() delegates correctly."""
        monkeypatch.setattr(sys, "platform", "darwin")
        ns = _make_helpers_ns()
        ns["is_upx_available"] = lambda: True
        assert ns["should_use_upx"]() is True

        ns["is_upx_available"] = lambda: False
        assert ns["should_use_upx"]() is False

    def test_never_calls_is_upx_available_on_windows(self, monkeypatch):
        """On win32, is_upx_available() must not be invoked at all."""
        monkeypatch.setattr(sys, "platform", "win32")
        ns = _make_helpers_ns()

        called = []
        ns["is_upx_available"] = lambda: called.append(True) or True

        result = ns["should_use_upx"]()
        assert result is False
        assert called == [], "is_upx_available() must not be called on Windows"


# ---------------------------------------------------------------------------
# 3. _read_version_from_pyproject()
# ---------------------------------------------------------------------------


class TestReadVersionFromPyproject:
    """``_read_version_from_pyproject()`` must parse semver strings robustly."""

    def test_parses_actual_pyproject_version(self):
        """Smoke-test: parses the real pyproject.toml in the repo."""
        ns = _make_helpers_ns(repo_root=_REPO_ROOT)
        result = ns["_read_version_from_pyproject"](_REPO_ROOT)
        major, minor, patch_v, build = result
        # We expect a proper semver tuple, not the zero fallback
        assert isinstance(major, int) and major >= 0
        assert isinstance(minor, int) and minor >= 0
        assert isinstance(patch_v, int) and patch_v >= 0
        assert build == 0, "Fourth element of the tuple must always be 0"
        assert (major, minor, patch_v) != (0, 0, 0), (
            "Version parsed from pyproject.toml should not be all-zeros"
        )

    def test_parses_semver_correctly(self, tmp_path):
        """Canonical semver ``major.minor.patch`` is mapped to a 4-tuple."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nversion = "1.23.4"\n', encoding="utf-8")
        ns = _make_helpers_ns(repo_root=tmp_path)
        assert ns["_read_version_from_pyproject"](tmp_path) == (1, 23, 4, 0)

    def test_parses_version_with_prerelease_suffix(self, tmp_path):
        """Pre-release suffix (``1.2.3rc1``) is ignored; only digits are kept."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nversion = "2.0.0rc1"\n', encoding="utf-8")
        ns = _make_helpers_ns(repo_root=tmp_path)
        assert ns["_read_version_from_pyproject"](tmp_path) == (2, 0, 0, 0)

    def test_returns_zero_tuple_when_pyproject_missing(self, tmp_path):
        """If pyproject.toml does not exist the function must return (0,0,0,0)."""
        ns = _make_helpers_ns(repo_root=tmp_path)  # tmp_path has no pyproject.toml
        assert ns["_read_version_from_pyproject"](tmp_path) == (0, 0, 0, 0)

    def test_returns_zero_tuple_when_version_key_absent(self, tmp_path):
        """pyproject.toml without a ``version =`` line returns (0,0,0,0)."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "my-app"\n', encoding="utf-8")
        ns = _make_helpers_ns(repo_root=tmp_path)
        assert ns["_read_version_from_pyproject"](tmp_path) == (0, 0, 0, 0)

    def test_returns_zero_tuple_for_non_numeric_version(self, tmp_path):
        """A version string with no leading digit group returns (0,0,0,0)."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nversion = "alpha"\n', encoding="utf-8")
        ns = _make_helpers_ns(repo_root=tmp_path)
        assert ns["_read_version_from_pyproject"](tmp_path) == (0, 0, 0, 0)

    def test_returns_zero_tuple_for_empty_file(self, tmp_path):
        """Empty pyproject.toml (no version key) returns (0,0,0,0)."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("", encoding="utf-8")
        ns = _make_helpers_ns(repo_root=tmp_path)
        assert ns["_read_version_from_pyproject"](tmp_path) == (0, 0, 0, 0)

    def test_result_is_four_tuple_of_ints(self, tmp_path):
        """Return type must always be a 4-tuple of ints."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nversion = "0.8.6"\n', encoding="utf-8")
        ns = _make_helpers_ns(repo_root=tmp_path)
        result = ns["_read_version_from_pyproject"](tmp_path)
        assert isinstance(result, tuple), "Must return a tuple"
        assert len(result) == 4, "Tuple must have exactly 4 elements"
        assert all(isinstance(x, int) for x in result), "All elements must be int"
