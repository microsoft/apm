"""End-to-end tests for `apm audit --external` external-scanner ingestion.

Exercises the full CLI path with a real fixture SARIF file (no network, no
vendor binary):
  - flag-off -> exit 2 with actionable message; native behavior unchanged.
  - flag-on  -> external findings merge into the report and drive exit code.
  - flag-on, info-only external findings -> non-gating exit 0.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

import apm_cli.config as _conf
from apm_cli.commands.audit import audit


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch):
    """Keep experimental flag state hermetic per test."""
    from apm_cli.config import _invalidate_config_cache

    _invalidate_config_cache()
    yield
    _invalidate_config_cache()


def _inject_flag(monkeypatch, enabled: bool) -> None:
    cfg: dict[str, Any] = {"experimental": {"external_scanners": enabled}}
    monkeypatch.setattr(_conf, "_config_cache", cfg)


def _write_sarif(path: Path, level: str = "error") -> None:
    path.write_text(
        json.dumps(
            {
                "version": "2.1.0",
                "runs": [
                    {
                        "tool": {"driver": {"name": "semgrep"}},
                        "results": [
                            {
                                "ruleId": "S1",
                                "level": level,
                                "message": {"text": "finding"},
                                "locations": [
                                    {
                                        "physicalLocation": {
                                            "artifactLocation": {"uri": "app/x.py"},
                                            "region": {"startLine": 1, "startColumn": 1},
                                        }
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_flag_off_exits_2(runner, monkeypatch, tmp_path):
    _inject_flag(monkeypatch, enabled=False)
    sarif = tmp_path / "r.sarif"
    _write_sarif(sarif)
    result = runner.invoke(
        audit, ["--external", "sarif", "--external-sarif", str(sarif), "-f", "json"]
    )
    assert result.exit_code == 2
    assert "external-scanners feature" in result.output


def test_flag_on_merges_critical_finding(runner, monkeypatch, tmp_path):
    _inject_flag(monkeypatch, enabled=True)
    sarif = tmp_path / "r.sarif"
    _write_sarif(sarif, level="error")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        audit, ["--external", "sarif", "--external-sarif", str(sarif), "-f", "json"]
    )
    assert result.exit_code == 1
    payload = json.loads(result.output[result.output.index("{") :])
    cats = [f["category"] for f in payload["findings"]]
    assert "sarif/S1" in cats
    assert payload["summary"]["critical"] == 1


def test_flag_on_info_only_is_non_gating(runner, monkeypatch, tmp_path):
    _inject_flag(monkeypatch, enabled=True)
    sarif = tmp_path / "r.sarif"
    _write_sarif(sarif, level="note")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        audit, ["--external", "sarif", "--external-sarif", str(sarif), "-f", "json"]
    )
    assert result.exit_code == 0


def test_external_sarif_requires_external_option(runner, monkeypatch, tmp_path):
    _inject_flag(monkeypatch, enabled=True)
    sarif = tmp_path / "r.sarif"
    _write_sarif(sarif)
    result = runner.invoke(audit, ["--external-sarif", str(sarif)])
    assert result.exit_code != 0
    assert "--external-sarif requires" in result.output


def test_external_rejected_with_strip(runner, monkeypatch, tmp_path):
    _inject_flag(monkeypatch, enabled=True)
    sarif = tmp_path / "r.sarif"
    _write_sarif(sarif)
    result = runner.invoke(
        audit, ["--external", "sarif", "--external-sarif", str(sarif), "--strip"]
    )
    assert result.exit_code != 0
    assert "cannot be combined with --strip" in result.output


def test_external_rejected_in_ci_mode(runner, monkeypatch, tmp_path):
    _inject_flag(monkeypatch, enabled=True)
    sarif = tmp_path / "r.sarif"
    _write_sarif(sarif)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(audit, ["--ci", "--external", "sarif", "--external-sarif", str(sarif)])
    assert result.exit_code != 0
    assert "does not support --external" in result.output


def test_unknown_scanner_name(runner, monkeypatch, tmp_path):
    _inject_flag(monkeypatch, enabled=True)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(audit, ["--external", "bogus", "-f", "json"])
    assert result.exit_code == 2
    assert "Unknown external scanner" in result.output
