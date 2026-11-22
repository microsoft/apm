"""Unit tests for the external SARIF-native scanner ingestion seam.

Covers:
  - sarif_to_findings: severity inversion, location/message extraction,
    rule-level inheritance, malformed-input fail-closed.
  - gate: is_enabled / require raises when the flag is off.
  - adapters: GenericSarifAdapter availability + parsing, SkillSpectorAdapter
    availability when the binary is absent.
  - registry: name resolution and unknown-name error.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Config injection fixture (mirrors tests/unit/core/test_experimental.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_config_cache():
    from apm_cli.config import _invalidate_config_cache

    _invalidate_config_cache()
    yield
    _invalidate_config_cache()


@pytest.fixture
def inject_config(monkeypatch):
    import apm_cli.config as _conf

    def _set(cfg: dict[str, Any]) -> None:
        monkeypatch.setattr(_conf, "_config_cache", cfg)

    return _set


# ---------------------------------------------------------------------------
# sarif_to_findings
# ---------------------------------------------------------------------------


def _sarif(results: list[dict], rules: list[dict] | None = None) -> dict:
    return {
        "version": "2.1.0",
        "runs": [
            {
                "tool": {"driver": {"name": "x", "rules": rules or []}},
                "results": results,
            }
        ],
    }


class TestSarifToFindings:
    def test_severity_inversion(self) -> None:
        from apm_cli.security.external.sarif_ingest import sarif_to_findings

        doc = _sarif(
            [
                {
                    "ruleId": "A",
                    "level": "error",
                    "message": {"text": "e"},
                    "locations": [
                        {
                            "physicalLocation": {
                                "artifactLocation": {"uri": "a.py"},
                                "region": {"startLine": 1, "startColumn": 1},
                            }
                        }
                    ],
                },
                {
                    "ruleId": "B",
                    "level": "warning",
                    "message": {"text": "w"},
                    "locations": [
                        {
                            "physicalLocation": {
                                "artifactLocation": {"uri": "b.py"},
                                "region": {"startLine": 2, "startColumn": 1},
                            }
                        }
                    ],
                },
                {
                    "ruleId": "C",
                    "level": "note",
                    "message": {"text": "n"},
                    "locations": [
                        {
                            "physicalLocation": {
                                "artifactLocation": {"uri": "c.py"},
                                "region": {"startLine": 3, "startColumn": 1},
                            }
                        }
                    ],
                },
            ]
        )
        out = sarif_to_findings(doc, tool_name="t")
        sev = {ff.category: ff.severity for v in out.values() for ff in v}
        assert sev == {"t/A": "critical", "t/B": "warning", "t/C": "info"}

    def test_unknown_and_none_level_map_to_info(self) -> None:
        from apm_cli.security.external.sarif_ingest import sarif_to_findings

        doc = _sarif(
            [
                {"ruleId": "X", "level": "bogus", "message": {"text": "x"}, "locations": []},
                {"ruleId": "Y", "message": {"text": "y"}, "locations": []},
            ]
        )
        out = sarif_to_findings(doc, tool_name="t")
        sevs = sorted(ff.severity for v in out.values() for ff in v)
        assert sevs == ["info", "info"]

    def test_rule_level_inheritance(self) -> None:
        from apm_cli.security.external.sarif_ingest import sarif_to_findings

        doc = _sarif(
            results=[{"ruleId": "R", "message": {"text": "m"}, "locations": []}],
            rules=[{"id": "R", "defaultConfiguration": {"level": "error"}}],
        )
        out = sarif_to_findings(doc, tool_name="t")
        finding = next(iter(out.values()))[0]
        assert finding.severity == "critical"

    def test_location_extraction(self) -> None:
        from apm_cli.security.external.sarif_ingest import sarif_to_findings

        doc = _sarif(
            [
                {
                    "ruleId": "R",
                    "level": "error",
                    "message": {"text": "m"},
                    "locations": [
                        {
                            "physicalLocation": {
                                "artifactLocation": {"uri": "src/x.py"},
                                "region": {"startLine": 42, "startColumn": 7},
                            }
                        }
                    ],
                }
            ]
        )
        out = sarif_to_findings(doc, tool_name="t")
        f = out["src/x.py"][0]
        assert (f.line, f.column, f.description) == (42, 7, "m")

    def test_missing_location_degrades_gracefully(self) -> None:
        from apm_cli.security.external.sarif_ingest import sarif_to_findings

        doc = _sarif([{"ruleId": "R", "level": "error", "message": {"text": "m"}}])
        out = sarif_to_findings(doc, tool_name="t")
        f = out["<unknown>"][0]
        assert (f.line, f.column) == (1, 1)

    def test_ansi_escape_codes_stripped_from_message(self) -> None:
        from apm_cli.security.external.sarif_ingest import sarif_to_findings

        doc = _sarif(
            [
                {
                    "ruleId": "R",
                    "level": "warning",
                    "message": {"text": "\x1b[31mexec() call detected\x1b[0m"},
                    "locations": [
                        {
                            "physicalLocation": {
                                "artifactLocation": {"uri": "s.py"},
                                "region": {"startLine": 5, "startColumn": 1},
                            }
                        }
                    ],
                }
            ]
        )
        out = sarif_to_findings(doc, tool_name="t")
        finding = out["s.py"][0]
        assert finding.description == "exec() call detected"

    def test_ansi_only_message_falls_back_to_no_message(self) -> None:
        from apm_cli.security.external.sarif_ingest import sarif_to_findings

        doc = _sarif(
            [
                {
                    "ruleId": "R",
                    "level": "warning",
                    "message": {"text": "\x1b[31m\x1b[0m"},
                    "locations": [],
                }
            ]
        )
        out = sarif_to_findings(doc, tool_name="t")
        finding = next(iter(out.values()))[0]
        assert finding.description == "(no message)"

    def test_not_a_sarif_document_raises(self) -> None:
        from apm_cli.security.external.base import ExternalScanError
        from apm_cli.security.external.sarif_ingest import sarif_to_findings

        with pytest.raises(ExternalScanError):
            sarif_to_findings({"not": "sarif"}, tool_name="t")

    def test_runs_not_a_list_raises(self) -> None:
        from apm_cli.security.external.base import ExternalScanError
        from apm_cli.security.external.sarif_ingest import sarif_to_findings

        with pytest.raises(ExternalScanError):
            sarif_to_findings({"runs": "nope"}, tool_name="t")


# ---------------------------------------------------------------------------
# gate
# ---------------------------------------------------------------------------


class TestGate:
    def test_disabled_by_default(self, inject_config) -> None:
        inject_config({})
        from apm_cli.security.external.gate import is_external_scanners_enabled

        assert is_external_scanners_enabled() is False

    def test_enabled_via_config(self, inject_config) -> None:
        inject_config({"experimental": {"external_scanners": True}})
        from apm_cli.security.external.gate import is_external_scanners_enabled

        assert is_external_scanners_enabled() is True

    def test_require_raises_when_off(self, inject_config) -> None:
        inject_config({})
        from apm_cli.security.external.gate import (
            ExternalScannersFeatureDisabledError,
            require_external_scanners_enabled,
        )

        with pytest.raises(ExternalScannersFeatureDisabledError):
            require_external_scanners_enabled()

    def test_require_passes_when_on(self, inject_config) -> None:
        inject_config({"experimental": {"external_scanners": True}})
        from apm_cli.security.external.gate import require_external_scanners_enabled

        require_external_scanners_enabled()  # no raise


# ---------------------------------------------------------------------------
# GenericSarifAdapter
# ---------------------------------------------------------------------------


class TestGenericSarifAdapter:
    def test_unavailable_without_file(self) -> None:
        from apm_cli.security.external.generic_sarif import GenericSarifAdapter

        ok, reason = GenericSarifAdapter().is_available()
        assert ok is False and "external-sarif" in reason

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
    def test_unavailable_when_binary_absent(self, monkeypatch) -> None:
        import apm_cli.security.external.skillspector as mod

        monkeypatch.setattr(mod.shutil, "which", lambda _name: None)
        ok, reason = mod.SkillSpectorAdapter().is_available()
        assert ok is False
        assert "PATH" in reason
        assert "Python >= 3.12" in reason
        assert "uv tool install" in reason

    def test_available_when_binary_present(self, monkeypatch) -> None:
        import apm_cli.security.external.skillspector as mod

        monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/skillspector")
        assert mod.SkillSpectorAdapter().is_available() == (True, None)

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


class TestRegistry:
    def test_resolves_skillspector(self) -> None:
        from apm_cli.security.external.registry import resolve_scanner
        from apm_cli.security.external.skillspector import SkillSpectorAdapter

        assert isinstance(resolve_scanner("skillspector"), SkillSpectorAdapter)

    def test_resolves_generic_sarif(self) -> None:
        from apm_cli.security.external.generic_sarif import GenericSarifAdapter
        from apm_cli.security.external.registry import resolve_scanner

        assert isinstance(resolve_scanner("sarif", sarif_file="x.sarif"), GenericSarifAdapter)

    def test_unknown_name_raises(self) -> None:
        from apm_cli.security.external.registry import resolve_scanner

        with pytest.raises(ValueError, match="Unknown external scanner"):
            resolve_scanner("nope")


# ---------------------------------------------------------------------------
# Flag registration
# ---------------------------------------------------------------------------


class TestFlagRegistration:
    def test_flag_registered(self) -> None:
        from apm_cli.core.experimental import FLAGS

        assert "external_scanners" in FLAGS
        assert FLAGS["external_scanners"].default is False
