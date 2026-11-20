"""Architecture regression for cross-process lifecycle serialization."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_RULE_ID = "install-deployment-lifecycle-serialization"
_CONFIG_PATH = "src/apm_cli/commands/config.py"
_WATCHER_PATH = "src/apm_cli/commands/compile/watcher.py"


def test_lifecycle_mutators_route_through_canonical_lock() -> None:
    """The registered lifecycle boundary must remain clean."""
    result = run_selected_rules(_ROOT, {_RULE_ID})
    assert result.failures == ()
    assert result.violations == ()


def test_lifecycle_guard_rejects_unserialized_mutator() -> None:
    """The static guard rejects removal of a lifecycle-lock route."""
    source = (_ROOT / _CONFIG_PATH).read_text(encoding="utf-8")
    mutated = source.replace("@serialized_lifecycle\ndef set(", "def set(", 1)
    assert mutated != source

    result = run_selected_rules(
        _ROOT,
        {_RULE_ID},
        source_overrides={_CONFIG_PATH: mutated},
    )

    assert any(
        violation.rule_id == _RULE_ID and "set must route" in violation.message
        for violation in result.violations
    )


def test_lifecycle_guard_rejects_unlocked_watch_startup() -> None:
    """The static guard covers the initial compilation in watch mode."""
    source = (_ROOT / _WATCHER_PATH).read_text(encoding="utf-8")
    mutated = source.replace(
        "operation = nullcontext() if dry_run else lifecycle_operation()",
        "operation = nullcontext()",
        1,
    )
    assert mutated != source

    result = run_selected_rules(
        _ROOT,
        {_RULE_ID},
        source_overrides={_WATCHER_PATH: mutated},
    )

    assert any(
        violation.rule_id == _RULE_ID and "_watch_mode must route" in violation.message
        for violation in result.violations
    )


_CLOSED_RULE_ID = "install-deployment-lifecycle-serialization-closed-world"


def test_closed_world_guard_rejects_serialized_command_missing_from_the_list() -> None:
    """A newly serialized command must join the closed serialized-route list."""
    path = "src/apm_cli/commands/doctor.py"
    source = (_ROOT / path).read_text(encoding="utf-8")
    mutated = source.replace("def doctor(", "@serialized_lifecycle\ndef doctor(", 1)
    assert mutated != source

    result = run_selected_rules(_ROOT, {_CLOSED_RULE_ID}, source_overrides={path: mutated})

    assert result.failures == ()
    assert [violation.message for violation in result.violations] == [
        "serialized CLI command doctor must be listed in SERIALIZED_LIFECYCLE_ROUTES"
    ]


def test_module_ownership_guard_rejects_a_stale_unowned_entry() -> None:
    """An owned module on the unowned allowlist is stale and breaks the ceiling."""
    path = ".apm/architecture/unowned-modules.json"
    source = (_ROOT / path).read_text(encoding="utf-8")
    mutated = source.replace(
        '"src/apm_cli/__init__.py",',
        '"src/apm_cli/__init__.py",\n    "src/apm_cli/core/script_trust.py",',
        1,
    )
    assert mutated != source
    rule_id = "registry_delegation.module_ownership"

    result = run_selected_rules(_ROOT, {rule_id}, source_overrides={path: mutated})

    assert result.failures == ()
    messages = [violation.message for violation in result.violations]
    assert any("stale unowned entry src/apm_cli/core/script_trust.py" in m for m in messages)
    assert any("the allowlist only shrinks" in m for m in messages)
