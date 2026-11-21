"""The strip-charset guard flags literal affixes but not deliberate char sets (#3715)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.architecture_linter.checks.contracts_strip_charset import (
    check_strip_charset_literals,
)
from scripts.architecture_linter.facts import FactsProvider

pytestmark = pytest.mark.component

_RULE = "contracts-tooling-strip-charset"
_SOURCE = """\
name = "x"
a = name.rstrip(".git")
b = name.lstrip("./")
c = b"x".strip(b".git")
d = name.strip("[]")
e = name.rstrip("\\r\\n")
f = name.rstrip("/")
g = name.strip()
h = name.removesuffix(".git")
"""


def _flagged_lines(tmp_path: Path, paths: tuple[str, ...]) -> list[tuple[str, int]]:
    for path in paths:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_SOURCE, encoding="utf-8")
    provider = FactsProvider(tmp_path, paths, registry=None)
    return sorted((v.path, v.line) for v in check_strip_charset_literals(provider, _RULE))


def test_flags_literal_affixes_including_bytes_but_not_allowlisted_char_sets(
    tmp_path: Path,
) -> None:
    assert _flagged_lines(tmp_path, ("src/apm_cli/mod.py",)) == [
        ("src/apm_cli/mod.py", 2),
        ("src/apm_cli/mod.py", 3),
        ("src/apm_cli/mod.py", 4),
    ]


def test_scans_shipped_code_and_scripts_but_not_tests(tmp_path: Path) -> None:
    flagged = _flagged_lines(
        tmp_path, ("src/apm_cli/a.py", "scripts/b.py", "tests/unit/c.py", "docs/d.py")
    )
    assert sorted({path for path, _line in flagged}) == ["scripts/b.py", "src/apm_cli/a.py"]
