"""Legacy filename classification that feeds the #3226 migration guard (pure logic cases)."""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest

from apm_cli.hook_contract import HookContractError
from apm_cli.integration.hook_file_routing import (
    raise_for_legacy_routing,
)

pytestmark = pytest.mark.unit


def test_migration_error_is_actionable_and_printable_ascii() -> None:
    with pytest.raises(HookContractError) as excinfo:
        raise_for_legacy_routing({"codex": ("hooks/hooks-claude.json",)}, "owner/repo")

    message = str(excinfo.value)
    assert message.isascii()
    assert "owner/repo: hook filename target routing was removed" in message
    assert "Target 'codex' would newly activate: hooks/hooks-claude.json" in message
    assert "target: <name>" in message
    docs = urlsplit(message.rsplit("See: ", 1)[1].split()[0])
    assert (docs.scheme, docs.hostname, docs.path, docs.fragment) == (
        "https",
        "microsoft.github.io",
        "/apm/producer/write-primitives/",
        "migrating-from-filename-routing",
    )


def test_no_conflicts_is_silent() -> None:
    raise_for_legacy_routing({}, "owner/repo")
