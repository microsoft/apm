"""Byte-for-byte snapshot test against microsoft/azure-skills.

The snapshot in ``tests/fixtures/azure-skills/`` was captured from
microsoft/azure-skills@bef1f05. This test asserts that running
``apm pack`` on the captured ``apm.yml`` produces a marketplace.json
whose SHA-256 matches the one shipped in that repo.

Marker: ``integration`` so it can be excluded from quick unit runs.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.commands.pack import pack_cmd


FIXTURES = Path(__file__).parent.parent / "fixtures" / "azure-skills"
EXPECTED_SHA256 = "02f76bfc0e5bbf7fdf1de1dda1f84c4da6e986913b6647973c0ffe39c1d5003b"


@pytest.mark.integration
def test_azure_skills_marketplace_byte_for_byte(tmp_path, monkeypatch):
    apm_src = FIXTURES / "apm.yml"
    expected_src = FIXTURES / ".claude-plugin" / "marketplace.json"
    assert apm_src.exists(), f"snapshot apm.yml missing at {apm_src}"
    assert expected_src.exists(), f"snapshot marketplace.json missing at {expected_src}"

    # Sanity-check the snapshot itself matches the documented hash. If
    # this fails, the fixture has drifted and needs to be re-captured.
    snapshot_sha = hashlib.sha256(expected_src.read_bytes()).hexdigest()
    assert snapshot_sha == EXPECTED_SHA256, (
        f"fixture marketplace.json SHA-256 drifted: {snapshot_sha}"
    )

    # Stage the apm.yml in a clean tempdir
    shutil.copy2(apm_src, tmp_path / "apm.yml")
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(pack_cmd, [])
    assert result.exit_code == 0, result.output

    out = tmp_path / ".claude-plugin" / "marketplace.json"
    assert out.exists(), "pack did not write .claude-plugin/marketplace.json"

    actual_sha = hashlib.sha256(out.read_bytes()).hexdigest()
    assert actual_sha == EXPECTED_SHA256, (
        "Generated marketplace.json drifted from azure-skills snapshot:\n"
        f"  expected: {EXPECTED_SHA256}\n"
        f"  actual:   {actual_sha}\n"
        f"  generated content:\n{out.read_text(encoding='utf-8')}"
    )
