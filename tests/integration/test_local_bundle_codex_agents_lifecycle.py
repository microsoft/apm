"""Local bundle agents are rendered to Codex TOML at install (#3714).

A bundle carries plugin-native Markdown agents. ``apm install <bundle>`` copies
them for Markdown-agent targets; for Codex, whose agent mapping is
``codex_agent``, it renders each agent to TOML with the agent integrator and
records the rendered hash, so ``apm audit --ci`` verifies the bytes Codex reads.
"""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from pathlib import Path

import pytest

from apm_cli.utils.content_hash import compute_file_hash
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.promise("P-OWN-bundle-native-layout-lowering", "P-INV-I1", "P-INV-I3"),
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
]

_NO_POLICY = "--no-policy"
_AGENT = ".codex/agents/rev.toml"
_EXPECTED_TOML = {
    "name": "rev",
    "description": "Review agent",
    "developer_instructions": "Review the diff.",
}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="ascii")


def _output(result: CommandResult) -> str:
    return " ".join((result.stdout + result.stderr).split())


class _World:
    def __init__(self, tmp_path: Path, apm_binary_path: Path) -> None:
        self.isolated = IsolatedApmEnvironment.create(tmp_path / "w", base_env=dict(os.environ))
        self.env = self.isolated.subprocess_env()
        self.root = self.isolated.work_root
        self.runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=240)

    def apm(self, cwd: Path, *args: str, expect: int | None = 0) -> CommandResult:
        result = self.runner.run(args, cwd=cwd, env=self.env)
        if expect is not None:
            assert result.returncode == expect, (
                f"apm {' '.join(args)} -> {result.returncode} (expected {expect})\n"
                f"stdout={result.stdout}\nstderr={result.stderr}"
            )
        return result

    def pack(self) -> Path:
        producer = self.root / "reviewer"
        _write(
            producer / "apm.yml",
            "name: reviewer\nversion: 1.0.0\ndescription: r\nlicense: MIT\n"
            "dependencies:\n  apm: []\n",
        )
        _write(
            producer / ".apm/agents/rev.agent.md",
            "---\nname: rev\ndescription: Review agent\n---\nReview the diff.\n",
        )
        self.apm(producer, "install", _NO_POLICY, "--target", "claude")
        self.apm(producer, "pack", "-o", str(self.root / "out"))
        bundle = self.root / "out" / "reviewer-1.0.0"
        assert (bundle / "agents/rev.agent.md").is_file()
        return bundle

    def project(self, name: str) -> Path:
        project = self.root / name
        _write(project / "apm.yml", f"name: {name}\nversion: 1.0.0\ntargets: [codex]\n")
        return project


def _assert_rendered(agent: Path, lockfile: Path, record: str) -> None:
    assert tomllib.loads(agent.read_text(encoding="utf-8")) == _EXPECTED_TOML
    lock = load_yaml(lockfile)
    assert record in lock["local_deployed_files"]
    assert lock["local_deployed_file_hashes"][record] == compute_file_hash(agent)


def _receipt_destinations(world: _World) -> set[str]:
    destinations: set[str] = set()
    for receipt in world.isolated.config_root.glob("deployment-receipts*/*.json"):
        destinations.update(
            json.loads(receipt.read_text(encoding="utf-8"))["destinations"].values()
        )
    return destinations


def _rendered_hash(packed: Path, agent: Path) -> str:
    """Hash of the agent integrator's Codex rendering of a packed agent."""
    from apm_cli.integration.agent_integrator import AgentIntegrator

    rendered, _links = AgentIntegrator._render_codex_agent(packed, agent)
    return f"sha256:{hashlib.sha256(rendered.encode('utf-8')).hexdigest()}"


def _corrupt_agent(bundle: Path) -> None:
    """Make the packed agent unrenderable while keeping the bundle manifest valid."""
    packed = b"---\nname: rev\n---\n\xff\xfe\n"
    (bundle / "agents/rev.agent.md").write_bytes(packed)
    lock = load_yaml(bundle / "apm.lock.yaml")
    lock["pack"]["bundle_files"]["agents/rev.agent.md"] = hashlib.sha256(packed).hexdigest()
    dump_yaml(lock, bundle / "apm.lock.yaml")


def test_codex_bundle_agent_project_lifecycle(tmp_path: Path, apm_binary_path: Path) -> None:
    world = _World(tmp_path, apm_binary_path)
    bundle = world.pack()
    install = ("install", str(bundle), "--target", "codex", _NO_POLICY)

    # --dry-run previews the agent and writes nothing.
    preview = world.project("preview")
    world.apm(preview, *install, "--dry-run")
    assert not (preview / ".codex").exists()
    assert not (preview / "apm.lock.yaml").exists()

    project = world.project("proj")
    agent = project / _AGENT
    world.apm(project, *install)
    _assert_rendered(agent, project / "apm.lock.yaml", _AGENT)
    # The rendered write is the bundle claim's deletion evidence (#4007).
    assert str(agent.resolve()) in _receipt_destinations(world)
    world.apm(project, "audit", "--ci", _NO_POLICY, "--no-fail-fast")
    rendered = agent.read_bytes()
    lock = (project / "apm.lock.yaml").read_bytes()

    # An identical reinstall adopts the rendered file: no collision, no churn.
    again = _output(world.apm(project, *install))
    assert "Skipped" not in again, again
    assert (project / "apm.lock.yaml").read_bytes() == lock

    # An edited destination is kept without --force and replaced with it.
    agent.write_bytes(rendered + b"# edited\n")
    kept = _output(world.apm(project, *install))
    assert f"Skipped {_AGENT}: file exists with different content" in kept, kept
    assert agent.read_bytes() == rendered + b"# edited\n"
    world.apm(project, *install, "--force")
    assert agent.read_bytes() == rendered
    world.apm(project, "audit", "--ci", _NO_POLICY, "--no-fail-fast")

    # A declarative frozen install keeps the bundle claim without lockfile churn.
    lock = (project / "apm.lock.yaml").read_bytes()
    world.apm(project, "install", "--frozen", _NO_POLICY)
    assert (project / "apm.lock.yaml").read_bytes() == lock
    assert agent.read_bytes() == rendered
    world.apm(project, "audit", "--ci", _NO_POLICY, "--no-fail-fast")


def test_codex_bundle_agent_render_failure_writes_nothing(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _World(tmp_path, apm_binary_path)
    bundle = world.pack()
    _corrupt_agent(bundle)
    project = world.project("proj")

    result = world.apm(project, "install", str(bundle), "--target", "codex", _NO_POLICY)
    text = _output(result)
    assert "Skipped packed bundle agent 'agents/rev.agent.md' for target codex" in text, text
    assert not (project / ".codex/agents").exists()
    lockfile = project / "apm.lock.yaml"
    lock = (load_yaml(lockfile) or {}) if lockfile.exists() else {}
    claims = [*(lock.get("local_deployed_files") or []), *(lock.get("deployments") or [])]
    assert not any(".codex/" in str(claim) for claim in claims), claims
    world.apm(project, "audit", "--ci", _NO_POLICY, "--no-fail-fast")


@pytest.mark.parametrize("codex_home", [False, True], ids=["default-home", "codex-home"])
def test_codex_bundle_agent_user_scope_lifecycle(
    tmp_path: Path, apm_binary_path: Path, codex_home: bool
) -> None:
    world = _World(tmp_path, apm_binary_path)
    bundle = world.pack()
    home = world.isolated.home
    agents = home / ".codex/agents"
    if codex_home:
        agents = tmp_path / "codex-home/agents"
        world.env = world.isolated.subprocess_env(overrides={"CODEX_HOME": str(agents.parent)})
    (home / ".codex").mkdir(exist_ok=True)
    install = ("install", "-g", str(bundle), "--target", "codex", _NO_POLICY)

    world.apm(world.root, *install)

    agent = agents / "rev.toml"
    assert tomllib.loads(agent.read_text(encoding="utf-8")) == _EXPECTED_TOML
    rendered_hash = _rendered_hash(bundle / "agents/rev.agent.md", agent)
    assert compute_file_hash(agent) == rendered_hash
    assert not (world.root / ".codex").exists()
    # The user lock lives in $APM_HOME; claims under HOME are home-relative.
    record = agent.relative_to(home).as_posix() if agent.is_relative_to(home) else agent.as_posix()
    lock = load_yaml(world.isolated.config_root / "apm.lock.yaml")
    assert lock["local_deployed_file_hashes"][record] == rendered_hash
    rendered = agent.read_bytes()

    # An edited destination is kept without --force and replaced with it.
    agent.write_bytes(rendered + b"# edited\n")
    kept = _output(world.apm(world.root, *install))
    assert "file exists with different content" in kept, kept
    assert agent.read_bytes() == rendered + b"# edited\n"
    world.apm(world.root, *install, "--force")
    assert agent.read_bytes() == rendered


def test_codex_bundle_agent_user_scope_lock_and_audit(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """The user lock in $APM_HOME records the home-relative claim and audits clean."""
    world = _World(tmp_path, apm_binary_path)
    bundle = world.pack()
    home = world.isolated.home
    (home / ".codex").mkdir(exist_ok=True)
    # Bundle installs never write apm.yml; a user-scope audit needs the user manifest.
    _write(world.isolated.config_root / "apm.yml", "name: user\nversion: 1.0.0\n")
    world.apm(world.root, "install", "-g", str(bundle), "--target", "codex", _NO_POLICY)

    lock = load_yaml(world.isolated.config_root / "apm.lock.yaml")
    assert _AGENT in lock["local_deployed_files"]
    assert lock["local_deployed_file_hashes"][_AGENT] == compute_file_hash(home / _AGENT)
    audit = world.apm(world.isolated.config_root, "audit", "--ci", _NO_POLICY, "--no-fail-fast")
    assert audit.returncode == 0, _output(audit)


def test_codex_bundle_agent_in_external_codex_home_audits_beside_another_target(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A -g agent under a $CODEX_HOME outside HOME stays a Codex claim (#4007).

    With a Claude claim already in the user lock and no manifest targets,
    ``audit --ci`` must still resolve, hash-check and drift-check the Codex agent.
    """
    world = _World(tmp_path, apm_binary_path)
    bundle = world.pack()
    home = world.isolated.home
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    world.env = world.isolated.subprocess_env(overrides={"CODEX_HOME": str(codex_home)})
    (home / ".claude").mkdir(exist_ok=True)
    _write(world.isolated.config_root / "apm.yml", "name: user\nversion: 1.0.0\n")
    for target in ("claude", "codex"):
        world.apm(world.root, "install", "-g", str(bundle), "--target", target, _NO_POLICY)

    agent = codex_home / "agents/rev.toml"
    assert tomllib.loads(agent.read_text(encoding="utf-8")) == _EXPECTED_TOML
    lock = load_yaml(world.isolated.config_root / "apm.lock.yaml")
    rows = {(row["target"], row["value"]) for row in lock["deployments"]}
    assert ("codex", "agents/rev.toml") in rows, rows
    assert ("claude", ".claude/agents/rev.md") in rows, rows
    assert str(agent.resolve()) in _receipt_destinations(world)
    audit = world.apm(world.isolated.config_root, "audit", "--ci", _NO_POLICY, "--no-fail-fast")
    assert audit.returncode == 0, _output(audit)

    # The claim keeps its target when a later bundle install rewrites the lock.
    world.apm(world.root, "install", "-g", str(bundle), "--target", "claude", _NO_POLICY)
    lock = load_yaml(world.isolated.config_root / "apm.lock.yaml")
    assert ("codex", "agents/rev.toml") in {
        (row["target"], row["value"]) for row in lock["deployments"]
    }
    world.apm(world.isolated.config_root, "audit", "--ci", _NO_POLICY, "--no-fail-fast")
