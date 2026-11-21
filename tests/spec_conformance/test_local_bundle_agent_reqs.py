"""Local bundle agents converted to Codex TOML honour the agent-conversion reqs (#3714).

``apm install <bundle>`` renders packed Markdown agents for a ``codex_agent``
target through the same Codex converter as a source install, so the
conversion requirements apply to the bundle path too.
"""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path

import pytest

from apm_cli.bundle.local_bundle import LocalBundleInfo
from apm_cli.core.scope import InstallScope
from apm_cli.install.services import integrate_local_bundle
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.utils.diagnostics import CATEGORY_AGENT_LOSSY_COMPILATION, DiagnosticCollector

pytestmark = [pytest.mark.component, pytest.mark.promise("req-tg-006", "req-tg-015")]


def _install_bundle_agent(tmp_path: Path, agent: str) -> tuple[dict, Path, DiagnosticCollector]:
    bundle = tmp_path / "bundle"
    packed = bundle / "agents" / "reviewer.agent.md"
    packed.parent.mkdir(parents=True)
    packed.write_bytes(agent.encode("utf-8"))
    project = tmp_path / "project"
    project.mkdir()
    diagnostics = DiagnosticCollector()
    result = integrate_local_bundle(
        LocalBundleInfo(
            source_dir=bundle,
            plugin_json={"id": "spec-bundle"},
            package_id="spec-bundle",
            lockfile={
                "pack": {
                    "bundle_files": {
                        "agents/reviewer.agent.md": hashlib.sha256(packed.read_bytes()).hexdigest()
                    }
                }
            },
            pack_targets=["claude"],
        ),
        project,
        targets=(KNOWN_TARGETS["codex"],),
        diagnostics=diagnostics,
        scope=InstallScope.PROJECT,
    )
    return result, project / ".codex" / "agents" / "reviewer.toml", diagnostics


@pytest.mark.req("req-tg-015")
def test_bundle_codex_agent_keeps_native_model_settings(tmp_path: Path) -> None:
    result, target, diagnostics = _install_bundle_agent(
        tmp_path,
        "---\nname: reviewer\ndescription: Review code\nmodel: gpt-5.6-sol\nmodel_reasoning_effort: high\n---\nReview.\n",
    )

    assert result["deployed_files"] == [".codex/agents/reviewer.toml"]
    doc = tomllib.loads(target.read_text(encoding="utf-8"))
    assert doc["model"] == "gpt-5.6-sol"
    assert doc["model_reasoning_effort"] == "high"
    assert diagnostics.by_category() == {}


@pytest.mark.req("req-tg-006")
def test_bundle_codex_agent_diagnoses_dropped_capability_restriction(tmp_path: Path) -> None:
    result, target, diagnostics = _install_bundle_agent(
        tmp_path, "---\nname: reviewer\ndescription: Review code\ntools: [read]\n---\nReview.\n"
    )

    assert result["deployed_files"] == [".codex/agents/reviewer.toml"]
    assert "tools" not in tomllib.loads(target.read_text(encoding="utf-8"))
    losses = diagnostics.by_category().get(CATEGORY_AGENT_LOSSY_COMPILATION, [])
    assert len(losses) == 1
    assert "field 'tools' was dropped" in losses[0].message
    assert losses[0].detail.startswith("Fix:")
