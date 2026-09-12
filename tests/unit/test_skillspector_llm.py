"""Unit tests for SkillSpector adapter argv construction + LLM/credential hygiene.

Covers the behaviour added by the scanner-config surface:

* ``--no-llm`` is present when LLM is off/unset and absent when ``llm=True``.
* allowlist-validated ``extra_args`` land before positional targets.
* ``is_available`` fails closed under ``--external-llm`` when no API key is set.
* the subprocess env drops LLM keys offline (env minimisation) and stderr is
  secret-redacted before reaching an error message.
* argv is always a list (no ``shell=True``).
* ``--baseline`` is allowlisted, must carry exactly one value, and may not
  point outside the working directory.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import apm_cli.security.external.skillspector as mod
from apm_cli.security.external.base import ExternalScanError
from apm_cli.security.external.options import ScannerOptions

_SARIF_EMPTY = '{"version": "2.1.0", "runs": [{"results": []}]}'


def _capture_run(monkeypatch, *, stdout=_SARIF_EMPTY, stderr="", returncode=0):
    """Patch ``shutil.which`` + ``subprocess.run`` and capture the argv/env."""
    monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/skillspector")
    captured: dict[str, object] = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)
    return captured


class TestArgvConstruction:
    def test_no_llm_present_by_default(self, monkeypatch) -> None:
        captured = _capture_run(monkeypatch)
        mod.SkillSpectorAdapter().scan([Path(".")], options=ScannerOptions())
        assert "--no-llm" in captured["cmd"]

    def test_no_llm_present_when_llm_false(self, monkeypatch) -> None:
        captured = _capture_run(monkeypatch)
        mod.SkillSpectorAdapter().scan([Path(".")], options=ScannerOptions(llm=False))
        assert "--no-llm" in captured["cmd"]

    def test_no_llm_absent_when_llm_true(self, monkeypatch) -> None:
        captured = _capture_run(monkeypatch)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        mod.SkillSpectorAdapter().scan([Path(".")], options=ScannerOptions(llm=True))
        assert "--no-llm" not in captured["cmd"]

    def test_extra_args_precede_targets(self, monkeypatch) -> None:
        captured = _capture_run(monkeypatch)
        mod.SkillSpectorAdapter().scan(
            [Path("pkg")],
            options=ScannerOptions(extra_args=("--model", "gpt-4o")),
        )
        cmd = list(captured["cmd"])
        assert cmd[-1] == "pkg"
        assert cmd.index("--model") < cmd.index("pkg")
        assert cmd[cmd.index("--model") + 1] == "gpt-4o"

    def test_targets_default_to_dot(self, monkeypatch) -> None:
        captured = _capture_run(monkeypatch)
        mod.SkillSpectorAdapter().scan([], options=ScannerOptions())
        assert captured["cmd"][-1] == "."

    def test_argv_is_a_list_not_shell(self, monkeypatch) -> None:
        captured = _capture_run(monkeypatch)
        mod.SkillSpectorAdapter().scan([Path(".")], options=ScannerOptions())
        assert isinstance(captured["cmd"], list)

    def test_disallowed_extra_arg_fails_closed(self, monkeypatch) -> None:
        _capture_run(monkeypatch)
        with pytest.raises(ExternalScanError, match=r"not\s+allowed"):
            mod.SkillSpectorAdapter().scan(
                [Path(".")], options=ScannerOptions(extra_args=("--output", "/tmp/x"))
            )


class TestLlmAvailability:
    def test_available_offline_without_key(self, monkeypatch) -> None:
        monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/skillspector")
        for var in mod._LLM_KEY_ENV_VARS:
            monkeypatch.delenv(var, raising=False)
        assert mod.SkillSpectorAdapter().is_available(options=ScannerOptions()) == (True, None)

    def test_llm_requires_key_fails_closed(self, monkeypatch) -> None:
        monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/skillspector")
        for var in mod._LLM_KEY_ENV_VARS:
            monkeypatch.delenv(var, raising=False)
        ok, reason = mod.SkillSpectorAdapter().is_available(options=ScannerOptions(llm=True))
        assert ok is False
        assert "API key" in reason

    def test_llm_available_with_key(self, monkeypatch) -> None:
        monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/skillspector")
        monkeypatch.setenv("NVIDIA_INFERENCE_KEY", "key-123")
        assert mod.SkillSpectorAdapter().is_available(options=ScannerOptions(llm=True)) == (
            True,
            None,
        )


class TestCredentialHygiene:
    def test_keys_stripped_when_offline(self, monkeypatch) -> None:
        captured = _capture_run(monkeypatch)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
        mod.SkillSpectorAdapter().scan([Path(".")], options=ScannerOptions(llm=False))
        assert "OPENAI_API_KEY" not in captured["env"]

    def test_keys_forwarded_when_llm_on(self, monkeypatch) -> None:
        captured = _capture_run(monkeypatch)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
        mod.SkillSpectorAdapter().scan([Path(".")], options=ScannerOptions(llm=True))
        assert captured["env"].get("OPENAI_API_KEY") == "sk-secret"

    def test_stderr_secret_redacted_in_error(self, monkeypatch) -> None:
        _capture_run(monkeypatch, stdout="", stderr="failed token=sk-abcdef123456", returncode=1)
        with pytest.raises(ExternalScanError) as exc:
            mod.SkillSpectorAdapter().scan([Path(".")], options=ScannerOptions())
        assert "sk-abcdef123456" not in str(exc.value)

    def test_timeout_raises_external_scan_error(self, monkeypatch) -> None:
        monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/skillspector")

        def _raise(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, mod._TIMEOUT_SECONDS)

        monkeypatch.setattr(mod.subprocess, "run", _raise)
        with pytest.raises(ExternalScanError, match="timed out"):
            mod.SkillSpectorAdapter().scan([Path(".")], options=ScannerOptions())


class TestBaselinePassthrough:
    """``--baseline`` carries a SkillSpector suppression baseline (issue #2331)."""

    def test_baseline_inside_working_directory_reaches_argv(self, monkeypatch, tmp_path) -> None:
        captured = _capture_run(monkeypatch)
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".skillspector-baseline.yaml").write_text("suppressions: []\n")

        mod.SkillSpectorAdapter().scan(
            [Path("pkg")],
            options=ScannerOptions(extra_args=("--baseline", ".skillspector-baseline.yaml")),
        )

        cmd = list(captured["cmd"])
        assert cmd[cmd.index("--baseline") + 1] == ".skillspector-baseline.yaml"
        assert cmd.index("--baseline") < cmd.index("pkg")
        assert cmd[-1] == "pkg"

    def test_baseline_inline_value_reaches_argv(self, monkeypatch, tmp_path) -> None:
        captured = _capture_run(monkeypatch)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "base.yaml").write_text("suppressions: []\n")

        mod.SkillSpectorAdapter().scan(
            [Path(".")], options=ScannerOptions(extra_args=("--baseline=base.yaml",))
        )
        assert "--baseline=base.yaml" in captured["cmd"]

    def test_baseline_without_value_rejected(self, monkeypatch, tmp_path) -> None:
        _capture_run(monkeypatch)
        monkeypatch.chdir(tmp_path)
        with pytest.raises(ExternalScanError, match=r"requires exactly one value"):
            mod.SkillSpectorAdapter().scan(
                [Path(".")], options=ScannerOptions(extra_args=("--baseline",))
            )

    def test_baseline_with_two_values_rejected(self, monkeypatch, tmp_path) -> None:
        _capture_run(monkeypatch)
        monkeypatch.chdir(tmp_path)
        with pytest.raises(ExternalScanError, match=r"second value"):
            mod.SkillSpectorAdapter().scan(
                [Path(".")],
                options=ScannerOptions(extra_args=("--baseline", "a.yaml", "b.yaml")),
            )

    def test_absolute_baseline_path_rejected(self, monkeypatch, tmp_path) -> None:
        _capture_run(monkeypatch)
        monkeypatch.chdir(tmp_path)
        outside = tmp_path.parent / "outside.yaml"
        with pytest.raises(ExternalScanError, match=r"scan directory"):
            mod.SkillSpectorAdapter().scan(
                [Path(".")],
                options=ScannerOptions(extra_args=("--baseline", str(outside))),
            )

    def test_relative_escape_baseline_path_rejected(self, monkeypatch, tmp_path) -> None:
        _capture_run(monkeypatch)
        monkeypatch.chdir(tmp_path)
        with pytest.raises(ExternalScanError, match=r"scan directory"):
            mod.SkillSpectorAdapter().scan(
                [Path(".")],
                options=ScannerOptions(extra_args=("--baseline", "../outside.yaml")),
            )

    def test_no_baseline_leaves_argv_unchanged(self, monkeypatch, tmp_path) -> None:
        captured = _capture_run(monkeypatch)
        monkeypatch.chdir(tmp_path)
        mod.SkillSpectorAdapter().scan([Path(".")], options=ScannerOptions())
        assert list(captured["cmd"])[1:] == ["scan", "--format", "sarif", "--no-llm", "."]
