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


# ---------------------------------------------------------------------------
# Scanner-option surface: orphan-flag guards + LLM/args passthrough
# ---------------------------------------------------------------------------


def test_external_llm_without_external_is_usage_error(runner, monkeypatch, tmp_path):
    _inject_flag(monkeypatch, enabled=True)
    result = runner.invoke(audit, ["--external-llm"])
    assert result.exit_code == 2
    assert "--external-llm" in result.output and "requires" in result.output


def test_no_external_llm_without_external_is_usage_error(runner, monkeypatch, tmp_path):
    _inject_flag(monkeypatch, enabled=True)
    result = runner.invoke(audit, ["--no-external-llm"])
    assert result.exit_code == 2
    assert "--external-llm" in result.output and "requires" in result.output


def test_external_args_without_external_is_usage_error(runner, monkeypatch, tmp_path):
    _inject_flag(monkeypatch, enabled=True)
    result = runner.invoke(audit, ["--external-args", "--model gpt-4o"])
    assert result.exit_code == 2
    assert "--external-args" in result.output and "requires" in result.output


def test_external_args_unmatched_quote_is_usage_error(runner, monkeypatch, tmp_path):
    """A malformed --external-args string surfaces as a usage error, not a traceback."""
    _inject_flag(monkeypatch, enabled=True)
    result = runner.invoke(audit, ["--external", "skillspector", "--external-args", "'unbalanced"])
    assert result.exit_code == 2
    assert "--external-args" in result.output and "could not be parsed" in result.output


def test_external_llm_threads_into_skillspector_argv(runner, monkeypatch, tmp_path):
    """--external-llm should drop --no-llm from the invoked skillspector argv."""
    _inject_flag(monkeypatch, enabled=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    import apm_cli.security.external.skillspector as ss

    captured: dict[str, Any] = {}

    def _fake_which(_binary):
        return "/usr/bin/skillspector"

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd

        class _R:
            returncode = 0
            stdout = json.dumps({"version": "2.1.0", "runs": []})
            stderr = ""

        return _R()

    monkeypatch.setattr(ss.shutil, "which", _fake_which)
    monkeypatch.setattr(ss.subprocess, "run", _fake_run)

    result = runner.invoke(audit, ["--external", "skillspector", "--external-llm", "-f", "json"])
    assert result.exit_code in (0, 1)
    assert "cmd" in captured, "skillspector subprocess was not invoked"
    assert "--no-llm" not in captured["cmd"]


def test_default_skillspector_argv_has_no_llm(runner, monkeypatch, tmp_path):
    """Without --external-llm the offline --no-llm default is preserved."""
    _inject_flag(monkeypatch, enabled=True)
    monkeypatch.chdir(tmp_path)

    import apm_cli.security.external.skillspector as ss

    captured: dict[str, Any] = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd

        class _R:
            returncode = 0
            stdout = json.dumps({"version": "2.1.0", "runs": []})
            stderr = ""

        return _R()

    monkeypatch.setattr(ss.shutil, "which", lambda _b: "/usr/bin/skillspector")
    monkeypatch.setattr(ss.subprocess, "run", _fake_run)

    result = runner.invoke(audit, ["--external", "skillspector", "-f", "json"])
    assert result.exit_code in (0, 1)
    assert "cmd" in captured
    assert "--no-llm" in captured["cmd"]
