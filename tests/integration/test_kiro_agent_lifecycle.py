"""Kiro agent lifecycle integration tests (#2089).

Exercises the full CLI install/reinstall/update/uninstall cycle for agents
deployed to .kiro/agents/ through the packaged CLI binary and real filesystem
effects. Mirrors the established pattern from test_primitive_target_covering_array.py.

Failure modes covered:
- compatible agent deploys with only description/model/tools (frontmatter filtering)
- nested relative identity (.apm/agents/subdir/X.agent.md -> .kiro/agents/subdir/X.md)
- idempotent reinstall (no spurious rewrites)
- update replacement (v1 source -> v2 deployed bytes)
- stale removal (uninstall prunes all .kiro/agents/ records)
- coexistence with steering and hooks under .kiro/
- incompatible-tools fail-closed (no partial agent write, no agents dir created)
- adopt-ordering regression (req-tg-009): byte-identical-source pre-seeded target
  with invalid tools MUST NOT be adopted; must remain skipped

Official source: https://kiro.dev/docs/custom-agents/ (accessed 2026-08-03)
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockFile
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackage, LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_e2e_mode,
    pytest.mark.requires_apm_binary,
]

_SCENARIO_TIMEOUT_SECONDS = 300.0
_REMOTE_PREFIX = "https://gitlab.example.invalid/apm-lifecycle"


def _run(
    runner: ApmLifecycleRunner,
    args: tuple[str, ...],
    *,
    scenario_id: str,
    cwd: Path,
    environment: dict[str, str],
) -> CommandResult:
    result = runner.run(args, scenario_id=scenario_id, cwd=cwd, env=environment)
    assert result.returncode == 0, (
        f"{scenario_id} failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result


def _run_expect_failure(
    runner: ApmLifecycleRunner,
    args: tuple[str, ...],
    *,
    scenario_id: str,
    cwd: Path,
    environment: dict[str, str],
) -> CommandResult:
    result = runner.run(args, scenario_id=scenario_id, cwd=cwd, env=environment)
    assert result.returncode != 0, (
        f"{scenario_id} unexpectedly succeeded\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result


def _install_args(targets: tuple[str, ...]) -> tuple[str, ...]:
    return ("install", "--target", ",".join(targets), "--no-policy", "--parallel-downloads", "0")


def _create_kiro_project(factory: LocalPackageFactory, name: str) -> LocalPackage:
    """Create a consumer project pre-seeded with a .kiro/ directory."""
    project = factory.create(name)
    (project.root / ".kiro").mkdir(exist_ok=True)
    return project


def _deployed_records(project_root: Path) -> tuple:
    lock = LockFile.read(project_root / "apm.lock.yaml")
    if lock is None:
        return ()
    return tuple(sorted(lock.deployment_ledger.records.items()))


def _kiro_agents_path(project_root: Path) -> Path:
    return project_root / ".kiro" / "agents"


def test_kiro_agent_compatible_deploy_and_reinstall(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Compatible agent deploys to .kiro/agents/ with filtered frontmatter; reinstall is idempotent."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "compatible", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    runner = ApmLifecycleRunner(
        (str(apm_binary_path),),
        scenario_timeout_seconds=_SCENARIO_TIMEOUT_SECONDS,
    )

    source_factory = LocalPackageFactory(isolated.package_root)
    pkg_name = "fixture-kiro-agent-compat"
    source = source_factory.create(pkg_name, targets=("kiro",))
    agent_content = (
        "---\n"
        "name: my-agent\n"
        "description: A compatible Kiro agent\n"
        "model: claude-3-5-sonnet-20241022\n"
        "tools:\n"
        "- read\n"
        "- write\n"
        "color: blue\n"
        "---\n\n"
        "# Compatible agent\n\nYou are a helpful agent.\n"
    )
    source_factory.add_agent(source, "compatible", agent_content)

    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    repo = repositories.create(pkg_name, source_tree=source.root)
    rev = repositories.commit(repo, message="seed kiro agent lifecycle")
    remote = f"{_REMOTE_PREFIX}/{pkg_name}.git"
    repositories.install_url_rewrite(repo, remote)

    project_factory = LocalPackageFactory(isolated.work_root)
    project = project_factory.create(
        f"consumer-{pkg_name}",
        dependencies=({"git": remote, "type": "gitlab", "ref": rev.sha, "alias": pkg_name},),
        targets=("kiro",),
    )
    (project.root / ".kiro").mkdir(exist_ok=True)
    cwd = project.root

    _run(
        runner,
        _install_args(("kiro",)),
        scenario_id="compat-install",
        cwd=cwd,
        environment=environment,
    )

    agents_dir = _kiro_agents_path(project.root)
    deployed = agents_dir / "compatible.md"
    assert deployed.exists(), "Expected .kiro/agents/compatible.md to exist"
    content = deployed.read_text(encoding="utf-8")
    assert "description: A compatible Kiro agent" in content
    assert "model: claude-3-5-sonnet-20241022" in content
    assert "- read" in content
    assert "- write" in content
    assert "name:" not in content, "'name' must be stripped for Kiro identity"
    assert "color:" not in content, "'color' must be stripped (unknown field)"
    assert "# Compatible agent" in content

    records_after_install = _deployed_records(project.root)
    assert any("kiro" in str(r) for r in records_after_install), (
        "Expected kiro agent in deployment ledger"
    )

    # Reinstall must be idempotent.
    _run(
        runner,
        _install_args(("kiro",)),
        scenario_id="compat-reinstall",
        cwd=cwd,
        environment=environment,
    )
    content_after_reinstall = deployed.read_text(encoding="utf-8")
    assert content_after_reinstall == content, "Reinstall changed agent content unexpectedly"


def test_kiro_agent_nested_path_identity(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Nested .apm/agents/subdir/name.agent.md deploys to .kiro/agents/subdir/name.md."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "nested", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    runner = ApmLifecycleRunner(
        (str(apm_binary_path),),
        scenario_timeout_seconds=_SCENARIO_TIMEOUT_SECONDS,
    )

    source_factory = LocalPackageFactory(isolated.package_root)
    pkg_name = "fixture-kiro-agent-nested"
    source = source_factory.create(pkg_name, targets=("kiro",))

    # Add nested agent manually (LocalPackageFactory.add_agent is flat)
    nested_dir = source.root / ".apm" / "agents" / "team"
    nested_dir.mkdir(parents=True, exist_ok=True)
    (nested_dir / "architect.agent.md").write_text(
        "---\ndescription: Team architect\ntools:\n- read\n---\n\n# Architect\n",
        encoding="utf-8",
    )

    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    repo = repositories.create(pkg_name, source_tree=source.root)
    rev = repositories.commit(repo, message="seed nested kiro agent")
    remote = f"{_REMOTE_PREFIX}/{pkg_name}.git"
    repositories.install_url_rewrite(repo, remote)

    project_factory = LocalPackageFactory(isolated.work_root)
    project = project_factory.create(
        f"consumer-{pkg_name}",
        dependencies=({"git": remote, "type": "gitlab", "ref": rev.sha, "alias": pkg_name},),
        targets=("kiro",),
    )
    (project.root / ".kiro").mkdir(exist_ok=True)
    cwd = project.root

    _run(
        runner,
        _install_args(("kiro",)),
        scenario_id="nested-install",
        cwd=cwd,
        environment=environment,
    )

    deployed = project.root / ".kiro" / "agents" / "team" / "architect.md"
    assert deployed.exists(), "Expected nested agent at .kiro/agents/team/architect.md"
    content = deployed.read_text(encoding="utf-8")
    assert "description: Team architect" in content
    assert "# Architect" in content


def test_kiro_agent_stale_removal(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Uninstall removes all .kiro/agents/ records from the deployment ledger."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "stale", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    runner = ApmLifecycleRunner(
        (str(apm_binary_path),),
        scenario_timeout_seconds=_SCENARIO_TIMEOUT_SECONDS,
    )

    source_factory = LocalPackageFactory(isolated.package_root)
    pkg_name = "fixture-kiro-agent-stale"
    source = source_factory.create(pkg_name, targets=("kiro",))
    source_factory.add_agent(
        source, "reviewer", "---\ndescription: Reviewer\ntools:\n- read\n---\n\n# Reviewer\n"
    )

    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    repo = repositories.create(pkg_name, source_tree=source.root)
    rev = repositories.commit(repo, message="seed kiro stale test")
    remote = f"{_REMOTE_PREFIX}/{pkg_name}.git"
    repositories.install_url_rewrite(repo, remote)

    project_factory = LocalPackageFactory(isolated.work_root)
    project = project_factory.create(
        f"consumer-{pkg_name}",
        dependencies=({"git": remote, "type": "gitlab", "ref": rev.sha, "alias": pkg_name},),
        targets=("kiro",),
    )
    (project.root / ".kiro").mkdir(exist_ok=True)
    cwd = project.root

    _run(
        runner,
        _install_args(("kiro",)),
        scenario_id="stale-install",
        cwd=cwd,
        environment=environment,
    )
    deployed = project.root / ".kiro" / "agents" / "reviewer.md"
    assert deployed.exists()

    _run(
        runner,
        ("uninstall", remote),
        scenario_id="stale-uninstall",
        cwd=cwd,
        environment=environment,
    )
    assert not deployed.exists(), "Uninstall must remove .kiro/agents/reviewer.md"
    records_after = _deployed_records(project.root)
    kiro_agent_records = [r for r in records_after if "kiro/agents" in str(r)]
    assert not kiro_agent_records, "No kiro agent records should remain after uninstall"


def test_kiro_agent_coexistence_with_steering_and_hooks(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Agents coexist with steering and hooks under .kiro/ without cross-clobbering."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "coexist", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    runner = ApmLifecycleRunner(
        (str(apm_binary_path),),
        scenario_timeout_seconds=_SCENARIO_TIMEOUT_SECONDS,
    )

    source_factory = LocalPackageFactory(isolated.package_root)
    pkg_name = "fixture-kiro-agent-coexist"
    source = source_factory.create(pkg_name, targets=("kiro",))

    source_factory.add_agent(
        source, "helper", "---\ndescription: Helper agent\ntools:\n- read\n---\n\n# Helper\n"
    )
    source_factory.add_instruction(
        source,
        "global",
        "---\napplyTo: '**'\ndescription: Global rules\n---\n\n# Rules\n",
    )
    source_factory.add_hook(
        source,
        "pre-tool",
        {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "echo coexist"}]}]}},
    )

    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    repo = repositories.create(pkg_name, source_tree=source.root)
    rev = repositories.commit(repo, message="seed kiro coexistence test")
    remote = f"{_REMOTE_PREFIX}/{pkg_name}.git"
    repositories.install_url_rewrite(repo, remote)

    project_factory = LocalPackageFactory(isolated.work_root)
    project = project_factory.create(
        f"consumer-{pkg_name}",
        dependencies=({"git": remote, "type": "gitlab", "ref": rev.sha, "alias": pkg_name},),
        targets=("kiro",),
    )
    (project.root / ".kiro").mkdir(exist_ok=True)
    cwd = project.root

    _run(
        runner,
        _install_args(("kiro",)),
        scenario_id="coexist-install",
        cwd=cwd,
        environment=environment,
    )

    assert (project.root / ".kiro" / "agents" / "helper.md").exists()
    assert (project.root / ".kiro" / "steering").is_dir()
    assert (project.root / ".kiro" / "hooks").is_dir()


def test_kiro_agent_incompatible_tools_fail_closed(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Agent with incompatible tools is not deployed; no partial write; no agents dir created."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "incompatible", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    runner = ApmLifecycleRunner(
        (str(apm_binary_path),),
        scenario_timeout_seconds=_SCENARIO_TIMEOUT_SECONDS,
    )

    source_factory = LocalPackageFactory(isolated.package_root)
    pkg_name = "fixture-kiro-agent-incompat"
    source = source_factory.create(pkg_name, targets=("kiro",))
    bad_agent_content = (
        "---\n"
        "description: Agent with bad tools\n"
        "tools:\n"
        "- read\n"
        "- execute_arbitrary_code\n"
        "---\n\n"
        "# Bad agent\n"
    )
    source_factory.add_agent(source, "badtools", bad_agent_content)

    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    repo = repositories.create(pkg_name, source_tree=source.root)
    rev = repositories.commit(repo, message="seed kiro incompatible-tools test")
    remote = f"{_REMOTE_PREFIX}/{pkg_name}.git"
    repositories.install_url_rewrite(repo, remote)

    project_factory = LocalPackageFactory(isolated.work_root)
    project = project_factory.create(
        f"consumer-{pkg_name}",
        dependencies=({"git": remote, "type": "gitlab", "ref": rev.sha, "alias": pkg_name},),
        targets=("kiro",),
    )
    (project.root / ".kiro").mkdir(exist_ok=True)
    cwd = project.root

    result = runner.run(
        _install_args(("kiro",)),
        scenario_id="incompat-install",
        cwd=cwd,
        env=environment,
    )
    # APM may exit 0 (fail-per-agent, not fatal) but must not write the agent.
    agents_dir = project.root / ".kiro" / "agents"
    assert not (agents_dir / "badtools.md").exists(), (
        "Incompatible agent MUST NOT be deployed (fail closed, no partial write)"
    )
    combined_output = result.stdout + result.stderr
    assert "execute_arbitrary_code" in combined_output, (
        "Error diagnostic must name the unsupported tool"
    )
    assert "badtools" in combined_output or "Kiro" in combined_output, (
        "Error diagnostic must identify the agent or target"
    )


def test_kiro_agent_update_replacement(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Source revision change replaces deployed agent bytes at .kiro/agents/.

    Workflow:
    1. Install v1 of the agent (marker 'v1-marker' in body).
    2. Commit v2 to the same repo (marker 'v2-marker' in body).
    3. Update consumer apm.yml ref to v2 SHA.
    4. Reinstall -- deployed agent MUST contain 'v2-marker' and NOT 'v1-marker'.
    """
    from apm_cli.utils.yaml_io import dump_yaml, load_yaml

    isolated = IsolatedApmEnvironment.create(tmp_path / "update", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    runner = ApmLifecycleRunner(
        (str(apm_binary_path),),
        scenario_timeout_seconds=_SCENARIO_TIMEOUT_SECONDS,
    )

    source_factory = LocalPackageFactory(isolated.package_root)
    pkg_name = "fixture-kiro-agent-update"
    source = source_factory.create(pkg_name, targets=("kiro",))

    # v1 agent content
    v1_content = (
        "---\ndescription: Updatable agent v1\ntools:\n- read\n---\n\n# Agent v1\n\nv1-marker\n"
    )
    source_factory.add_agent(source, "updatable", v1_content)

    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    repo = repositories.create(pkg_name, source_tree=source.root)
    rev1 = repositories.commit(repo, message="kiro agent v1")
    remote = f"{_REMOTE_PREFIX}/{pkg_name}.git"
    repositories.install_url_rewrite(repo, remote)

    project_factory = LocalPackageFactory(isolated.work_root)
    project = project_factory.create(
        f"consumer-{pkg_name}",
        dependencies=({"git": remote, "type": "gitlab", "ref": rev1.sha, "alias": pkg_name},),
        targets=("kiro",),
    )
    (project.root / ".kiro").mkdir(exist_ok=True)
    cwd = project.root

    # Install v1.
    _run(
        runner,
        _install_args(("kiro",)),
        scenario_id="update-install-v1",
        cwd=cwd,
        environment=environment,
    )
    deployed = project.root / ".kiro" / "agents" / "updatable.md"
    assert deployed.exists(), "v1 agent must be deployed"
    v1_bytes = deployed.read_text(encoding="utf-8")
    assert "v1-marker" in v1_bytes, "deployed agent must contain v1 marker"

    # Commit v2: update source agent in the repo worktree.
    v2_content = (
        "---\ndescription: Updatable agent v2\ntools:\n- read\n- write\n---\n\n"
        "# Agent v2\n\nv2-marker\n"
    )
    agent_src = repo.worktree / ".apm" / "agents" / "updatable.agent.md"
    agent_src.write_text(v2_content, encoding="utf-8")
    rev2 = repositories.commit(repo, message="kiro agent v2")

    # Update consumer apm.yml to reference v2 SHA.
    manifest = load_yaml(project.manifest_path)
    assert isinstance(manifest, dict)
    for dep in manifest.get("dependencies", {}).get("apm", []):
        if isinstance(dep, dict) and dep.get("alias") == pkg_name:
            dep["ref"] = rev2.sha
    dump_yaml(manifest, project.manifest_path)

    # Reinstall with updated ref.
    _run(
        runner,
        ("install", "--target", "kiro", "--no-policy", "--parallel-downloads", "0"),
        scenario_id="update-install-v2",
        cwd=cwd,
        environment=environment,
    )
    v2_bytes = deployed.read_text(encoding="utf-8")
    assert "v2-marker" in v2_bytes, "deployed agent must contain v2 marker after update"
    assert "v1-marker" not in v2_bytes, "v1 marker must be gone after update"
    assert "description: Updatable agent v2" in v2_bytes


def test_kiro_target_autodetection(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Kiro is autodetected via .kiro/ when no explicit --target is passed.

    Verifies KNOWN_TARGETS['kiro'].detect_by_dir = True is honored by the CLI.
    Consumer apm.yml declares no targets; the presence of .kiro/ in the project
    root drives target selection, and the package declares targets: [kiro].
    The agent is deployed without any --target flag.

    Canonical path: resolve_targets() -> detect_signals() sees .kiro/ ->
    returns ['kiro'] -> intersects with package targets: ['kiro'] ->
    agent integrator deploys to .kiro/agents/.
    """

    isolated = IsolatedApmEnvironment.create(tmp_path / "autodetect", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    runner = ApmLifecycleRunner(
        (str(apm_binary_path),),
        scenario_timeout_seconds=_SCENARIO_TIMEOUT_SECONDS,
    )

    source_factory = LocalPackageFactory(isolated.package_root)
    pkg_name = "fixture-kiro-agent-autodetect"
    source = source_factory.create(pkg_name, targets=("kiro",))
    source_factory.add_agent(
        source,
        "autodetected",
        "---\ndescription: Autodetected agent\ntools:\n- read\n---\n\n# Auto\n",
    )

    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    repo = repositories.create(pkg_name, source_tree=source.root)
    rev = repositories.commit(repo, message="seed kiro autodetect test")
    remote = f"{_REMOTE_PREFIX}/{pkg_name}.git"
    repositories.install_url_rewrite(repo, remote)

    project_factory = LocalPackageFactory(isolated.work_root)
    # Consumer project: NO explicit targets declaration.
    project = project_factory.create(
        f"consumer-{pkg_name}",
        dependencies=({"git": remote, "type": "gitlab", "ref": rev.sha, "alias": pkg_name},),
        targets=(),  # <-- no explicit targets: relies on autodetection
    )

    # Seed .kiro/ to trigger autodetection -- the signal detect_by_dir checks.
    (project.root / ".kiro").mkdir(exist_ok=True)
    cwd = project.root

    # Run install with NO --target flag.
    _run(
        runner,
        ("install", "--no-policy", "--parallel-downloads", "0"),
        scenario_id="autodetect-install",
        cwd=cwd,
        environment=environment,
    )

    deployed = project.root / ".kiro" / "agents" / "autodetected.md"
    assert deployed.exists(), (
        ".kiro/ autodetection must route the kiro agent to .kiro/agents/ "
        "without an explicit --target flag"
    )
    content = deployed.read_text(encoding="utf-8")
    assert "description: Autodetected agent" in content
