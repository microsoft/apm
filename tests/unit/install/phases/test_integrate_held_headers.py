"""A batch refusal before integration still prints the held package headers (#3994).

Acquisition holds each package's ``[+]`` header until its integration tree
starts. A preflight refusal or the skill-baseline refusal returns before that,
so the phase must drain the held headers itself.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.install.phases import integrate

_COUNTERS = (
    "installed_count",
    "unpinned_count",
    "total_prompts_integrated",
    "total_agents_integrated",
    "total_skills_integrated",
    "total_sub_skills_promoted",
    "total_instructions_integrated",
    "total_commands_integrated",
    "total_hooks_integrated",
    "total_links_resolved",
)


def _ctx(tmp_path: Path) -> SimpleNamespace:
    dep = SimpleNamespace(
        is_local=True,
        local_path="./pkg",
        get_install_path=lambda root: root / "pkg",
        get_unique_key=lambda: "_local/pkg",
    )
    return SimpleNamespace(
        deps_to_install=[dep],
        all_apm_deps=[],
        apm_modules_dir=tmp_path / "apm_modules",
        callback_failures=set(),
        logger=None,
        skill_baseline_refused=False,
        frozen=False,
        **dict.fromkeys(_COUNTERS, 0),
    )


@pytest.fixture
def emitted(monkeypatch) -> list[str]:
    printed: list[str] = []

    def prepare(source):
        source._held_header_lines = [lambda: printed.append("[+] _local/pkg")]
        return object(), None

    monkeypatch.setattr(integrate, "make_dependency_source", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(integrate, "prepare_integration_materialization", prepare)
    monkeypatch.setattr("apm_cli.install.lsp.integration.preflight_lsp_targets", lambda ctx: None)
    monkeypatch.setattr(integrate, "_fail_explicit_target_without_deploy", lambda ctx: None)
    return printed


def test_preflight_refusal_still_prints_the_acquired_header(tmp_path, monkeypatch, emitted):
    def refuse(_materialized):
        raise RuntimeError("refused")

    monkeypatch.setattr(integrate, "preflight_agent_plugin_materializations", refuse)
    with pytest.raises(RuntimeError, match="refused"):
        integrate.run(_ctx(tmp_path))
    assert emitted == ["[+] _local/pkg"]


def test_skill_baseline_refusal_still_prints_the_acquired_header(tmp_path, monkeypatch, emitted):
    def refuse_baseline(ctx, _materialized):
        ctx.skill_baseline_refused = True
        return {}, None

    monkeypatch.setattr(integrate, "preflight_agent_plugin_materializations", lambda m: None)
    monkeypatch.setattr(integrate, "preflight_skill_materializations", refuse_baseline)
    integrate.run(_ctx(tmp_path))
    assert emitted == ["[+] _local/pkg"]
