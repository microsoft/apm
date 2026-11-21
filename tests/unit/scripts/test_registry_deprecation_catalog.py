"""The deprecation-catalog guard rejects warning text spelled outside the catalog (#4019)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.architecture_linter.checks.registry_deprecation_catalog import (
    DEPRECATION_CATALOG,
    check_deprecation_catalog_text,
)
from scripts.architecture_linter.facts import FactsProvider

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-user-facing-output-diagnostics")]

_RULE = "registry_delegation.deprecation_catalog"
_CATALOG = """\
Deprecation(
    key="unpack",
    template="'apm unpack' is deprecated and will be removed in a future release. {remedy}",
)
Deprecation(
    key="vars",
    template="Deprecated: <VAR> used in {count} servers ({names}). Migrate to ${{VAR}} now.",
)
"""


def _flagged(tmp_path: Path, files: dict[str, str]) -> list[tuple[str, int, str]]:
    files = {DEPRECATION_CATALOG: _CATALOG, **files}
    for path, text in files.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    provider = FactsProvider(tmp_path, tuple(files), registry=None)
    return sorted(
        (v.path, v.line, v.message) for v in check_deprecation_catalog_text(provider, _RULE)
    )


def test_routed_call_sites_and_unrelated_text_pass(tmp_path: Path) -> None:
    source = (
        'msg = deprecation_message("unpack")\n'
        'help = "[Deprecated] Extract a bundle; this command will be removed."\n'
    )
    assert _flagged(tmp_path, {"src/apm_cli/commands/pack.py": source}) == []


def test_plain_and_fstring_copies_are_flagged_with_the_catalog_key(tmp_path: Path) -> None:
    source = (
        "x = 1\n"
        "a = \"'apm unpack' is deprecated and will be removed in a future release.\"\n"
        'b = f"Deprecated: <VAR> used in {n} servers ({s}). Migrate to ${{VAR}} now."\n'
    )
    flagged = _flagged(tmp_path, {"src/apm_cli/mod.py": source})

    assert sorted({(line, message.rsplit(" ", 1)[-1]) for _path, line, message in flagged}) == [
        (2, "deprecation_message('unpack')"),
        (3, "deprecation_message('vars')"),
    ]


def test_only_shipped_code_outside_the_catalog_is_scanned(tmp_path: Path) -> None:
    copy = "a = \"'apm unpack' is deprecated and will be removed in a future release.\"\n"
    flagged = _flagged(tmp_path, {"tests/unit/test_x.py": copy, "scripts/y.py": copy})

    assert flagged == []


def test_an_emptied_catalog_fails_closed(tmp_path: Path) -> None:
    flagged = _flagged(tmp_path, {DEPRECATION_CATALOG: "CATALOG = ()\n"})

    assert [(path, line) for path, line, _message in flagged] == [(DEPRECATION_CATALOG, 1)]
