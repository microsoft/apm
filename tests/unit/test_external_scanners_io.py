"""Unit tests for the external SARIF-native scanner ingestion seam (filesystem, process and CLI boundary cases)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.component

# ---------------------------------------------------------------------------
# Config injection fixture (mirrors tests/unit/core/test_experimental.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_config_cache():
    from apm_cli.config import _invalidate_config_cache

    _invalidate_config_cache()
    yield
    _invalidate_config_cache()


# ---------------------------------------------------------------------------
# sarif_to_findings
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# gate
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GenericSarifAdapter
# ---------------------------------------------------------------------------


class TestGenericSarifAdapter:
    def test_unavailable_missing_file(self, tmp_path: Path) -> None:
        from apm_cli.security.external.generic_sarif import GenericSarifAdapter

        ok, reason = GenericSarifAdapter(tmp_path / "no.sarif").is_available()
        assert ok is False and "not found" in reason

    def test_scan_parses_file(self, tmp_path: Path) -> None:
        from apm_cli.security.external.generic_sarif import GenericSarifAdapter

        sarif = tmp_path / "r.sarif"
        sarif.write_text(
            '{"version":"2.1.0","runs":[{"tool":{"driver":{"name":"s"}},'
            '"results":[{"ruleId":"R","level":"error","message":{"text":"bad"},'
            '"locations":[{"physicalLocation":{"artifactLocation":{"uri":"a.py"},'
            '"region":{"startLine":1,"startColumn":1}}}]}]}]}',
            encoding="utf-8",
        )
        adapter = GenericSarifAdapter(sarif)
        assert adapter.is_available()[0] is True
        out = adapter.scan([tmp_path])
        assert out["a.py"][0].severity == "critical"

    def test_scan_invalid_json_raises(self, tmp_path: Path) -> None:
        from apm_cli.security.external.base import ExternalScanError
        from apm_cli.security.external.generic_sarif import GenericSarifAdapter

        sarif = tmp_path / "bad.sarif"
        sarif.write_text("{not json", encoding="utf-8")
        with pytest.raises(ExternalScanError):
            GenericSarifAdapter(sarif).scan([tmp_path])


# ---------------------------------------------------------------------------
# SkillSpectorAdapter
# ---------------------------------------------------------------------------


class TestSkillSpectorAdapter:
    def test_scan_passes_no_llm_flag(self, monkeypatch, tmp_path: Path) -> None:
        """--no-llm must be part of the command so scans work without an API key."""
        import apm_cli.security.external.skillspector as mod

        monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/skillspector")

        captured_cmd: list[str] = []
        sarif = (
            '{"version":"2.1.0","runs":[{"tool":{"driver":{"name":"s","rules":[]}},"results":[]}]}'
        )

        def fake_run(cmd, **_kwargs):
            captured_cmd.extend(cmd)
            return mod.subprocess.CompletedProcess(cmd, 0, stdout=sarif, stderr="")

        monkeypatch.setattr(mod.subprocess, "run", fake_run)

        mod.SkillSpectorAdapter().scan([tmp_path])
        assert "--no-llm" in captured_cmd

    def test_scan_non_json_stdout_surfaces_first_line(self, monkeypatch, tmp_path: Path) -> None:
        """When SkillSpector writes an error to stdout, the message is surfaced."""
        import apm_cli.security.external.skillspector as mod
        from apm_cli.security.external.base import ExternalScanError

        monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/skillspector")

        error_text = "Error: NVIDIA_API_KEY not set. Please configure an API key."

        def fake_run(cmd, **_kwargs):
            return mod.subprocess.CompletedProcess(cmd, 1, stdout=error_text, stderr="")

        monkeypatch.setattr(mod.subprocess, "run", fake_run)

        with pytest.raises(ExternalScanError, match=r"NVIDIA_API_KEY not set"):
            mod.SkillSpectorAdapter().scan([tmp_path])

    def test_scan_non_json_stdout_sanitises_non_ascii(self, monkeypatch, tmp_path: Path) -> None:
        """Non-printable / non-ASCII chars in vendor stdout are replaced with '?'."""
        import apm_cli.security.external.skillspector as mod
        from apm_cli.security.external.base import ExternalScanError

        monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/skillspector")

        # ANSI escape + non-ASCII char embedded in vendor error output.
        error_text = "\x1b[31mError\x1b[0m: caf\u00e9 failure"

        def fake_run(cmd, **_kwargs):
            return mod.subprocess.CompletedProcess(cmd, 1, stdout=error_text, stderr="")

        monkeypatch.setattr(mod.subprocess, "run", fake_run)

        with pytest.raises(ExternalScanError, match=r"\?.*Error.*\?.*caf\?"):
            mod.SkillSpectorAdapter().scan([tmp_path])


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Flag registration
# ---------------------------------------------------------------------------
