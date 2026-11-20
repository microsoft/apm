"""Unit tests for SkillSpector adapter argv construction + LLM/credential hygiene (pure logic cases)."""

from __future__ import annotations

import pytest

import apm_cli.security.external.skillspector as mod
from apm_cli.security.external.options import ScannerOptions

pytestmark = pytest.mark.unit


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
