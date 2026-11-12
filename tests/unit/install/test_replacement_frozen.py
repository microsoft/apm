"""Frozen replacement-table comparison uses declared intent, not source access."""

from __future__ import annotations

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.install.plan import lockfile_satisfies_replacements
from apm_cli.models.dependency.replacement import parse_replace_block

pytestmark = pytest.mark.unit

_LOCAL = {
    "for": {"git": "https://github.com/example/catalog", "path": "packages/base"},
    "with": {"path": "../never-present"},
}
_FORK = {
    "for": {"git": "https://github.com/example/other"},
    "with": {"git": "https://github.com/consumer/other", "ref": "patch"},
}


@pytest.mark.parametrize(
    ("locked", "declared"),
    [(None, None), ([], []), ([_LOCAL], [_LOCAL]), ([_LOCAL, _FORK], [_FORK, _LOCAL])],
)
def test_frozen_replacement_table_accepts_identical_normalized_intent(
    locked: list[dict] | None, declared: list[dict] | None
) -> None:
    rules = None if declared is None else parse_replace_block(declared)
    accepted, reasons = lockfile_satisfies_replacements(LockFile(replacements=locked), rules)
    assert accepted is True
    assert reasons == []


@pytest.mark.parametrize(
    ("locked", "declared"),
    [
        (None, []),
        ([], None),
        ([], [_LOCAL]),
        ([_LOCAL], []),
        ([_LOCAL], [_FORK]),
        ([_LOCAL, _FORK], [_LOCAL]),
        ([_LOCAL], [_LOCAL, _FORK]),
        (
            [_LOCAL],
            [{"for": _LOCAL["for"], "with": {"path": "../different-never-present"}}],
        ),
    ],
)
def test_frozen_replacement_table_rejects_added_removed_or_changed_rules(
    locked: list[dict] | None, declared: list[dict] | None
) -> None:
    rules = None if declared is None else parse_replace_block(declared)
    accepted, reasons = lockfile_satisfies_replacements(LockFile(replacements=locked), rules)
    assert accepted is False
    assert reasons
    assert all("replacement" in reason.lower() for reason in reasons)


def test_frozen_replacement_table_rejects_corrupt_duplicate_lock_rules() -> None:
    locked = LockFile(replacements=[_LOCAL, _LOCAL])
    accepted, reasons = lockfile_satisfies_replacements(locked, parse_replace_block([_LOCAL]))
    assert accepted is False
    assert any("duplicate" in reason.lower() for reason in reasons)
