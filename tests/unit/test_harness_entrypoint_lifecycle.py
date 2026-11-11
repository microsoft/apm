"""An installed provider can import native implementations during CLI bootstrap."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.utils.isolated_apm_environment import IsolatedApmEnvironment


@pytest.mark.component
def test_installed_adapter_deploys_native_agents_and_compiles_context(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=os.environ)
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    metadata = plugins / "sample_harness-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: sample-harness\nVersion: 1.0\n", encoding="utf-8"
    )
    (metadata / "entry_points.txt").write_text(
        "[apm.harnesses]\nsample = sample_harness:adapter\n", encoding="utf-8"
    )
    (plugins / "sample_harness.py").write_text(
        """from apm_cli.harnesses import HarnessAdapter, PrimitiveMapping, TargetCapability, TargetProfile
from apm_cli.integration.agent_integrator import AgentIntegrator
from apm_cli.adapters.client.gemini import GeminiClientAdapter
from apm_cli.core.apm_yml import CANONICAL_TARGETS
from apm_cli.core.target_detection import EXPLICIT_ONLY_TARGETS

class NativeAgents(AgentIntegrator):
    def integrate_agents_for_target(self, target, package_info, project_root, **kwargs):
        result = super().integrate_agents_for_target(target, package_info, project_root, **kwargs)
        for path in result.target_paths:
            content = path.read_text(encoding="utf-8")
            path.write_text(content + "\\nSample native rendering\\n", encoding="utf-8")
        return result

def adapter():
    capability = TargetCapability(
        name="sample", aliases=("sample-alias",), description="Synthetic sample harness",
        in_all=False, explicit_only=True, experimental_flag=None, mcp_only=False,
        primitive_profile="sample", compile_family="agents", runtimes=(),
        commands=frozenset({"compile", "install", "update"}),
    )
    return HarnessAdapter(
        capability=capability,
        profile=TargetProfile(capability=capability, root_dir=".sample", detect_by_dir=False,
            primitives={"agents": PrimitiveMapping("agents", ".md", "claude_agent")}),
        integrators={"agents": NativeAgents},
    )
""",
        encoding="utf-8",
    )
    project = isolated.work_root / "project"
    project.mkdir()
    (project / "apm.yml").write_text(
        "name: sample-consumer\nversion: 1.0.0\ntarget: sample\n", encoding="utf-8"
    )
    for kind, filename, content in (
        (
            "agents",
            "review.agent.md",
            "---\ndescription: Review the sample\n---\nRead this source.\n",
        ),
        (
            "instructions",
            "rule.instructions.md",
            "---\ndescription: Sample rule\n---\nPreserve sample invariants.\n",
        ),
    ):
        path = project / ".apm" / kind / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    foreign = project / ".sample" / "agents" / "foreign.md"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("User-owned agent\n", encoding="utf-8")
    environment = isolated.subprocess_env()
    environment["PYTHONPATH"] = os.pathsep.join(
        (environment["PYTHONPATH"], str(plugins), str(repo / "src"))
    )
    for args in (("install",), ("compile", "--target", "sample-alias")):
        result = subprocess.run(
            [sys.executable, "-m", "apm_cli.entrypoint", *args],
            cwd=project,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    assert foreign.read_text(encoding="utf-8") == "User-owned agent\n"
    rendered = [path for path in foreign.parent.glob("*.md") if path != foreign]
    assert any("Sample native rendering" in path.read_text(encoding="utf-8") for path in rendered)
    assert "Preserve sample invariants." in (project / "AGENTS.md").read_text(encoding="utf-8")
    assert not (project / "CLAUDE.md").exists()
