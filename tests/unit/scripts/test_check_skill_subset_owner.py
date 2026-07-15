"""Unit tests for scripts/check_skill_subset_owner.py.

The script is not inside the ``apm_cli`` package, so it is imported directly
from its file path (see ``tests/unit/test_ssl_cert_hook.py`` for the same
pattern used elsewhere in this repo).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_skill_subset_owner.py"


def _load_module() -> ModuleType:
    """Import the checker script as a standalone module.

    The module is registered in ``sys.modules`` before execution so that
    ``dataclasses`` (used for ``Violation``) can resolve ``from __future__
    import annotations`` string annotations against the module's own
    namespace.
    """
    spec = importlib.util.spec_from_file_location("check_skill_subset_owner", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def checker() -> ModuleType:
    return _load_module()


# ---------------------------------------------------------------------------
# Fixture sources
# ---------------------------------------------------------------------------

# A renamed helper that reimplements the full skill_subset_filter_tokens()
# algorithm: slash normalization + PurePosixPath leaf extraction + token-set
# collection. This is the exact shape the retired `_skill_subset_name_filter`
# duplicate had, under a different name, which is why the lexical grep guard
# alone missed it.
_DUPLICATE_HELPER_SOURCE = '''
from pathlib import PurePosixPath


def promotion_tokens(skill_subset):
    """Reimplements the canonical owner's normalization under a new name."""
    if not skill_subset:
        return None
    tokens = set()
    for skill_name in skill_subset:
        raw_name = str(skill_name).strip()
        normalized_path = raw_name.replace("\\\\", "/")
        leaf_name = PurePosixPath(normalized_path).name
        tokens.add(raw_name)
        tokens.add(normalized_path)
        if leaf_name:
            tokens.add(leaf_name)
    return tokens or None
'''

# A consumer that correctly delegates to the canonical owner.
_CANONICAL_CALL_SOURCE = """
from apm_cli.models.dependency.subsets import skill_subset_filter_tokens


def get_name_filter(skill_subset):
    return skill_subset_filter_tokens(skill_subset)
"""

# A function that only normalizes slashes (one of the three signals) but does
# not extract a PurePosixPath leaf or collect a token set. This must not be
# flagged -- it proves the checker requires the full combination, not any one
# signal in isolation, keeping the false-positive rate low.
_PARTIAL_SIGNAL_SOURCE = '''
def normalize_slashes(value):
    """Only does slash normalization -- not the full duplicate algorithm."""
    return str(value).replace("\\\\", "/")
'''

# A function that calls skill_subset_filter_tokens() directly AND
# independently reimplements the full three-signal algorithm in the same
# body. Delegating to the canonical owner must not exempt the function --
# the reimplementation alongside it is the actual duplication this checker
# exists to catch.
_OWNER_CALL_PLUS_DUPLICATE_SOURCE = '''
from pathlib import PurePosixPath

from apm_cli.models.dependency.subsets import skill_subset_filter_tokens


def merged_tokens(skill_subset):
    """Calls the canonical owner but also reimplements its algorithm."""
    tokens = skill_subset_filter_tokens(skill_subset) or set()
    for skill_name in skill_subset or []:
        raw_name = str(skill_name).strip()
        normalized_path = raw_name.replace("\\\\", "/")
        leaf_name = PurePosixPath(normalized_path).name
        tokens.add(raw_name)
        tokens.add(normalized_path)
        if leaf_name:
            tokens.add(leaf_name)
    return tokens or None
'''

# The same three-signal algorithm as _DUPLICATE_HELPER_SOURCE, but with the
# leaf-path construction and the ``.name`` read split across two statements
# instead of chained in one expression. This is the split-statement
# evasion: the leaf-path call is assigned to a local variable first, and
# ``.name`` is read off that variable later in the function.
_SPLIT_STATEMENT_DUPLICATE_SOURCE = '''
from pathlib import PurePosixPath


def flattened_tokens(skill_subset):
    """Reimplements the algorithm with leaf extraction split across statements."""
    if not skill_subset:
        return None
    tokens = set()
    for skill_name in skill_subset:
        raw_name = str(skill_name).strip()
        normalized_path = raw_name.replace("\\\\", "/")
        path = PurePosixPath(normalized_path)
        leaf_name = path.name
        tokens.add(raw_name)
        tokens.add(normalized_path)
        if leaf_name:
            tokens.add(leaf_name)
    return tokens or None
'''

# A function that assigns a variable from a call that is NOT one of the
# leaf-path constructors, then reads ``.name`` off it. This must not be
# flagged even though it also normalizes slashes and collects a token set --
# proving the split-statement tracking only recognizes the specific
# leaf-path callees, not any assignment followed by a ``.name`` read.
_SPLIT_STATEMENT_UNRELATED_NAME_SOURCE = '''
class Entry:
    def __init__(self, name):
        self.name = name


def build_tokens(skill_subset):
    """A .name read on a non-leaf-path variable must not count as leaf extraction."""
    tokens = set()
    for skill_name in skill_subset or []:
        raw_name = str(skill_name).strip()
        normalized_path = raw_name.replace("\\\\", "/")
        entry = Entry(raw_name)
        leaf_name = entry.name
        tokens.add(raw_name)
        tokens.add(normalized_path)
        if leaf_name:
            tokens.add(leaf_name)
    return tokens or None
'''


def _write(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# find_violations()
# ---------------------------------------------------------------------------


def test_renamed_helper_reimplementing_algorithm_is_reported(tmp_path: Path, checker) -> None:
    """A renamed helper with the same three-signal algorithm must be flagged."""
    path = _write(tmp_path, "duplicate.py", _DUPLICATE_HELPER_SOURCE)

    violations = checker.find_violations([path])

    assert len(violations) == 1
    assert violations[0].qualname == "promotion_tokens"
    assert violations[0].path == path


def test_direct_canonical_owner_call_is_allowed(tmp_path: Path, checker) -> None:
    """A function that calls skill_subset_filter_tokens() directly is clean."""
    path = _write(tmp_path, "consumer.py", _CANONICAL_CALL_SOURCE)

    violations = checker.find_violations([path])

    assert violations == []


def test_partial_signal_alone_is_not_flagged(tmp_path: Path, checker) -> None:
    """A single matching signal (slash normalization only) is not enough."""
    path = _write(tmp_path, "partial.py", _PARTIAL_SIGNAL_SOURCE)

    violations = checker.find_violations([path])

    assert violations == []


def test_owner_call_plus_duplicate_in_same_function_is_reported(tmp_path: Path, checker) -> None:
    """Delegating to the canonical owner must not exempt a same-function duplicate.

    A function that calls skill_subset_filter_tokens() and *also*
    independently reimplements the three-signal algorithm in its own body
    is still a duplication and must be flagged -- the direct call does not
    grant a function-wide exemption.
    """
    path = _write(tmp_path, "merged.py", _OWNER_CALL_PLUS_DUPLICATE_SOURCE)

    violations = checker.find_violations([path])

    assert len(violations) == 1
    assert violations[0].qualname == "merged_tokens"


def test_split_statement_leaf_extraction_is_reported(tmp_path: Path, checker) -> None:
    """Leaf extraction split across two statements must still be detected.

    ``path = PurePosixPath(normalized)`` followed by ``leaf = path.name`` is
    the same leaf-extraction signal as the chained ``PurePosixPath(...).name``
    form, just split across statements; it must not evade detection.
    """
    path = _write(tmp_path, "split.py", _SPLIT_STATEMENT_DUPLICATE_SOURCE)

    violations = checker.find_violations([path])

    assert len(violations) == 1
    assert violations[0].qualname == "flattened_tokens"


def test_name_read_on_non_leaf_path_variable_is_not_flagged(tmp_path: Path, checker) -> None:
    """A ``.name`` read on a variable not assigned from a leaf-path call is not leaf extraction.

    Proves the split-statement tracking is narrowly scoped to the specific
    leaf-path constructors (``PurePosixPath``/``Path``/``PureWindowsPath``/
    ``PurePath``), not any assignment followed by any ``.name`` read.
    """
    path = _write(tmp_path, "unrelated_name.py", _SPLIT_STATEMENT_UNRELATED_NAME_SOURCE)

    violations = checker.find_violations([path])

    assert violations == []


def test_missing_file_raises_configured_path_error(tmp_path: Path, checker) -> None:
    """A configured consumer path that does not exist must fail closed.

    A missing (or renamed) consumer path is a misconfiguration, not "nothing
    to check": silently reporting zero violations for it would let the guard
    be evaded by pointing it at a path that no longer exists.
    """
    missing = tmp_path / "does-not-exist.py"

    with pytest.raises(checker.ConfiguredPathError, match=r"does-not-exist\.py"):
        checker.find_violations([missing])


def test_directory_path_raises_configured_path_error(tmp_path: Path, checker) -> None:
    """A configured consumer path that is a directory, not a regular file, fails closed."""
    directory = tmp_path / "a-directory"
    directory.mkdir()

    with pytest.raises(checker.ConfiguredPathError, match=r"a-directory"):
        checker.find_violations([directory])


def test_violation_message_is_actionable(tmp_path: Path, checker) -> None:
    """The rendered violation names the file, line, function, and owner."""
    path = _write(tmp_path, "duplicate.py", _DUPLICATE_HELPER_SOURCE)

    violations = checker.find_violations([path])

    message = violations[0].render()
    assert str(path) in message
    assert "promotion_tokens" in message
    assert "skill_subset_filter_tokens" in message


# ---------------------------------------------------------------------------
# CLI (main())
# ---------------------------------------------------------------------------


def test_cli_returns_nonzero_and_prints_offender_for_duplicate_fixture(
    tmp_path: Path, checker, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI must exit nonzero and print an actionable offender line."""
    path = _write(tmp_path, "duplicate.py", _DUPLICATE_HELPER_SOURCE)

    exit_code = checker.main([str(path)])

    assert exit_code != 0
    captured = capsys.readouterr()
    assert "promotion_tokens" in captured.out
    assert str(path) in captured.out


def test_cli_returns_zero_for_clean_consumer_fixture(
    tmp_path: Path, checker, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI must exit zero when a fixture only calls the canonical owner."""
    path = _write(tmp_path, "consumer.py", _CANONICAL_CALL_SOURCE)

    exit_code = checker.main([str(path)])

    assert exit_code == 0


def test_cli_returns_zero_for_real_consumers(checker) -> None:
    """The two real consumers wired into the Bash guard must pass today."""
    integrator = REPO_ROOT / "src/apm_cli/integration/skill_integrator.py"
    exporter = REPO_ROOT / "src/apm_cli/bundle/plugin_exporter.py"

    exit_code = checker.main([str(integrator), str(exporter)])

    assert exit_code == 0


def test_cli_returns_nonzero_and_prints_diagnostic_for_missing_target(
    tmp_path: Path, checker, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI must fail closed (nonzero) with an actionable message for a missing path.

    A missing configured consumer path must not be silently treated as
    "no violations found" -- it must be reported as a failure so a renamed
    or deleted consumer file cannot silently disable the guard.
    """
    missing = tmp_path / "does-not-exist.py"

    exit_code = checker.main([str(missing)])

    assert exit_code != 0
    captured = capsys.readouterr()
    assert str(missing) in captured.out
    assert "missing" in captured.out.lower()
