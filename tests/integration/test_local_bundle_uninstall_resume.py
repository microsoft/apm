"""An interrupted bundle hook retirement resumes from its note (#5291, #4987).

PROMISE: P-INV-I7 -- the resume note is written only once retirement starts,
before any hook or script is removed, so an interrupt there leaves the hooks
deployed and a retried uninstall finishes the retirement.

The bundle is installed with the real binary; the interrupted uninstall runs
in-process so Ctrl-C lands right after the note is written.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.integration._marketplace_pack_kit import MarketplacePackWorld, output
from tests.integration.test_local_bundle_hooks_lifecycle import (
    _claims_of,
    _copilot_bundle_consumer,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.component,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I7"),
]

_PENDING = Path("apm_modules") / ".local-bundle-pending.json"


def test_an_uninstall_interrupted_after_its_note_resumes_on_retry(
    tmp_path: Path, apm_binary_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C after the note: hooks stay, the note says retiring, a retry removes them."""
    from click.testing import CliRunner

    from apm_cli.cli import cli

    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    consumer, hook = _copilot_bundle_consumer(world, tmp_path, "interrupted-consumer")
    (script,) = [c for c in _claims_of(consumer) if c.endswith("/x.sh")]

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    with monkeypatch.context() as patched:
        for key, value in world.env.items():
            patched.setenv(key, value)
        patched.setattr("apm_cli.install.local_bundle_hook_trust.retire_bundle_hooks", interrupt)
        patched.chdir(consumer)
        result = CliRunner().invoke(cli, ["uninstall", "hookpkg"], env=world.env)

    assert result.exit_code != 0, result.output
    note = json.loads((consumer / _PENDING).read_text(encoding="utf-8"))["bundles"]["hookpkg"]
    assert note["retiring"] == ["hooks"]
    assert (consumer / hook).is_file() and (consumer / script).is_file()

    text = output(world.apm(consumer, "uninstall", "hookpkg"))

    assert "Removed hooks from local bundle 'hookpkg'" in text, text
    assert not (consumer / hook).exists() and not (consumer / script).exists()
    assert script not in _claims_of(consumer)
