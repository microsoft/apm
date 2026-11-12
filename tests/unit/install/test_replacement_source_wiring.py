"""Replacement declarations survive lock assembly even when dormant."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.install.phases.lockfile import LockfileBuilder
from apm_cli.models.dependency.replacement import parse_replace_block, replacement_rule_to_dict

pytestmark = pytest.mark.unit

_RULE = {
    "for": {"git": "https://github.com/example/base"},
    "with": {"path": "../not-present"},
}


@pytest.mark.parametrize("declared", [None, [], [_RULE]])
def test_lock_assembly_retains_complete_root_replacement_intent(
    declared: list[dict] | None,
) -> None:
    rules = None if declared is None else parse_replace_block(declared)
    context = SimpleNamespace(apm_package=SimpleNamespace(replace_rules=rules))
    lockfile = LockFile()
    LockfileBuilder(context)._attach_replacement_table(lockfile)
    expected = None if rules is None else [replacement_rule_to_dict(rule) for rule in rules]
    assert lockfile.replacements == expected


@pytest.mark.parametrize(
    ("existing", "declared", "changed"),
    [
        (None, None, False),
        (None, [], True),
        (None, [_RULE], True),
        ([], None, True),
        ([], [], False),
        ([_RULE], [_RULE], False),
        ([_RULE], [], True),
    ],
)
def test_lock_assembly_does_not_skip_dormant_rule_changes(
    existing: list[dict] | None, declared: list[dict] | None, changed: bool
) -> None:
    rules = None if declared is None else parse_replace_block(declared)
    context = SimpleNamespace(
        apm_package=SimpleNamespace(replace_rules=rules),
        existing_lockfile=None if existing is None else LockFile(replacements=existing),
    )
    assert LockfileBuilder(context)._replacement_table_changed() is changed
