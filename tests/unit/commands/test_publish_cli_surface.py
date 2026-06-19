"""CLI-surface regression traps for ``apm publish`` authoring nudges.

Mirrors the pack-path coverage in ``test_pack_cli_surface.py``: the
authoring license nudge (#1777) must fire on the AUTHORING path when the
user's own ``apm.yml`` declares no license, and stay silent when one is
declared. The warn must never block publish.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

import apm_cli.commands.publish as publish_mod
from apm_cli.commands.publish import publish_cmd


def _enable_registry(monkeypatch) -> None:
    # The license nudge fires after the experimental-feature gate; neutralize
    # the gate so the test exercises the warn wiring, not feature flags.
    monkeypatch.setattr(publish_mod, "require_package_registry_enabled", lambda *a, **k: None)


def test_publish_warns_when_no_license_declared(tmp_path, monkeypatch):
    _enable_registry(monkeypatch)
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        Path("apm.yml").write_text("name: demo\nversion: 1.0.0\n")
        result = runner.invoke(publish_cmd, ["--package", "demo/demo", "--dry-run"])
        combined = result.output + (result.stderr or "")
        assert "license:" in combined, combined


def test_publish_silent_when_license_declared(tmp_path, monkeypatch):
    _enable_registry(monkeypatch)
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        Path("apm.yml").write_text("name: demo\nversion: 1.0.0\nlicense: MIT\n")
        result = runner.invoke(publish_cmd, ["--package", "demo/demo", "--dry-run"])
        combined = result.output + (result.stderr or "")
        assert "the SBOM will record NOASSERTION" not in combined
