"""Component proof: every remediation command is a real apm command line (#4090).

Each catalog entry's ``command`` must resolve, through the live Click tree,
to an existing command whose every flag is one of its options, and its
``fixes_promise`` must name the state it clears. Executed installed-CLI
proofs for install, lock and deploy-state entries live in
``tests/integration/test_remediation_advice_lifecycle.py``.
"""

from __future__ import annotations

import pytest

from apm_cli.core.remediation import catalog
from tests.utils.click_tree import resolve_line

pytestmark = [
    pytest.mark.unit,
    pytest.mark.promise("P-OWN-remediation-advice"),
]

_ENTRIES = tuple(sorted(catalog().values(), key=lambda entry: entry.id))


@pytest.mark.parametrize("entry", _ENTRIES, ids=lambda entry: entry.id)
def test_remediation_command_resolves_to_a_real_command(entry) -> None:
    command = entry.command.replace("{", "<").replace("}", ">")
    resolution = resolve_line(command)

    assert resolution.ok, (entry.id, entry.command, resolution)
    assert entry.fixes_promise.strip(), entry.id
    assert entry.fixes_promise.isascii() and entry.command.isascii(), entry.id
