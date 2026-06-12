"""Regression tests for #1345: watch mode must forward the resolved target.

The non-watch ``apm compile`` path correctly omits ``GEMINI.md`` when
``apm.yml`` declares ``targets: [claude, cursor]`` (#1019/#1074). Before
this fix, the watch path bypassed target resolution and let
``CompilationConfig.from_apm_yml`` fall back to the all-families default,
silently regenerating ``GEMINI.md`` on every recompile.

These tests pin the contract:

1. ``APMFileHandler._recompile`` calls ``CompilationConfig.from_apm_yml``
   with ``target=<effective_target>``.
2. The captured target is the exact frozenset the resolver produced --
   and ``should_compile_gemini_md`` returns False for it, so the bug
   cannot silently come back.
3. When no target is configured (``effective_target=None``), the watcher
   forwards ``None`` and lets ``from_apm_yml`` keep its dataclass default.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.commands.compile.watcher import APMFileHandler
from apm_cli.core.target_detection import should_compile_gemini_md


@pytest.fixture
def fake_logger():
    return SimpleNamespace(
        progress=MagicMock(),
        success=MagicMock(),
        error=MagicMock(),
        warning=MagicMock(),
    )


def _make_handler(fake_logger, effective_target):
    return APMFileHandler(
        output="AGENTS.md",
        chatmode=None,
        no_links=False,
        dry_run=False,
        logger=fake_logger,
        effective_target=effective_target,
    )


def test_recompile_forwards_frozenset_target(fake_logger):
    """`targets: [claude, cursor]` -> frozenset({'claude','agents'}) is forwarded."""
    effective = frozenset({"claude", "agents"})
    handler = _make_handler(fake_logger, effective)

    with (
        patch(
            "apm_cli.commands.compile.watcher.CompilationConfig.from_apm_yml"
        ) as mock_from_apm_yml,
        patch("apm_cli.commands.compile.watcher.AgentsCompiler") as mock_compiler_cls,
    ):
        mock_from_apm_yml.return_value = MagicMock()
        mock_compiler_cls.return_value.compile.return_value = SimpleNamespace(
            success=True, output_path="AGENTS.md", errors=[]
        )

        handler._recompile("dummy.md")

    assert mock_from_apm_yml.call_count == 1
    captured_kwargs = mock_from_apm_yml.call_args.kwargs
    assert "target" in captured_kwargs, (
        "Watcher must forward target= to CompilationConfig.from_apm_yml; "
        "missing it is the #1345 regression."
    )
    assert captured_kwargs["target"] == effective

    # Outcome assertion: no GEMINI.md is emitted for this target. If a
    # future change reintroduces the all-families fanout, this fails.
    assert should_compile_gemini_md(captured_kwargs["target"]) is False


def test_recompile_forwards_none_when_no_target_configured(fake_logger):
    """Auto-detect / unset target case: forward None, no surprise override."""
    handler = _make_handler(fake_logger, None)

    with (
        patch(
            "apm_cli.commands.compile.watcher.CompilationConfig.from_apm_yml"
        ) as mock_from_apm_yml,
        patch("apm_cli.commands.compile.watcher.AgentsCompiler") as mock_compiler_cls,
    ):
        mock_from_apm_yml.return_value = MagicMock()
        mock_compiler_cls.return_value.compile.return_value = SimpleNamespace(
            success=True, output_path="AGENTS.md", errors=[]
        )

        handler._recompile("dummy.md")

    assert mock_from_apm_yml.call_args.kwargs["target"] is None


def test_recompile_forwards_single_string_target(fake_logger):
    """Single-target case (e.g. `--target claude`) is forwarded verbatim."""
    handler = _make_handler(fake_logger, "claude")

    with (
        patch(
            "apm_cli.commands.compile.watcher.CompilationConfig.from_apm_yml"
        ) as mock_from_apm_yml,
        patch("apm_cli.commands.compile.watcher.AgentsCompiler") as mock_compiler_cls,
    ):
        mock_from_apm_yml.return_value = MagicMock()
        mock_compiler_cls.return_value.compile.return_value = SimpleNamespace(
            success=True, output_path="AGENTS.md", errors=[]
        )

        handler._recompile("dummy.md")

    assert mock_from_apm_yml.call_args.kwargs["target"] == "claude"
    assert should_compile_gemini_md("claude") is False
