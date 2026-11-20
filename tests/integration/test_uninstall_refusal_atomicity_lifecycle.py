"""Installed-CLI proofs: a refused uninstall changes nothing (#4185).

``apm uninstall`` validates what it will need before its first write: the
survivors' org-policy trust and every recorded path. A refusal leaves
apm.yml, apm.lock.yaml and every deployed file byte-identical.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration._fault_injection_kit import build_world, evidence

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]


def test_policy_refusal_mid_uninstall_changes_nothing(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Survivor trust is resolved before any write, so a blocking policy refuses cleanly."""
    world = build_world(tmp_path, apm_binary_path, packages=("alpha", "beta"))
    manifest = world.project / "apm.yml"
    # No git remote: an enforceable org policy cannot be resolved, and the
    # project asks that to block.
    manifest.write_text(
        manifest.read_text(encoding="ascii") + "policy:\n  fetch_failure_default: block\n",
        encoding="ascii",
    )
    before = world.snapshot(".")

    refused = world.apm("uninstall", "./packages/alpha")

    assert refused.returncode == 1, evidence(refused)
    assert "Blocked by org policy" in refused.stdout + refused.stderr, evidence(refused)
    assert "no APM writes were made" in " ".join(refused.stdout.split()), evidence(refused)
    diff = before.diff(world.snapshot("."))
    assert not (diff.added | diff.removed | diff.changed), diff
    world.assert_user_file_intact()
