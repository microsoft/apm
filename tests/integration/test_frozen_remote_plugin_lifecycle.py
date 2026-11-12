"""Installed-CLI cold frozen restoration from real producer lockfiles."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepository, LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_REMOTE = "https://gitlab.example.invalid/frozen/plugin.git"
_SKILL = b"---\nname: frozen-skill\ndescription: Frozen fixture\n---\n# Frozen skill\n"
_ROOT_MCP = {
    "name": "root-server",
    "command": "fixture-root",
    "transport": "stdio",
    "registry": False,
}
_PLUGIN_MCP = {"command": "fixture-plugin", "args": ["--readonly"]}


@dataclass
class _Scenario:
    """Real producer evidence and a separate consumer with no APM state."""

    runner: ApmLifecycleRunner
    producer: Path
    consumer: Path
    environment: dict[str, str]
    producer_environment: dict[str, str]
    repositories: LocalGitRepositoryFactory
    repository: LocalGitRepository
    pins: set[str]
    expected_type: str

    def freeze(self, *, success: bool = True, only_mcp: bool = False) -> str:
        """Run the installed CLI and retain exact inputs across either outcome."""
        before = {
            name: (self.consumer / name).read_bytes() for name in ("apm.yml", "apm.lock.yaml")
        }
        result = self.runner.run(
            (
                "install",
                "--frozen",
                "--no-policy",
                "--parallel-downloads",
                "0",
                *(("--only", "mcp") if only_mcp else ()),
            ),
            scenario_id="cold-whole-plugin-consumer",
            cwd=self.consumer,
            env=self.environment,
        )
        output = result.stdout + result.stderr
        assert result.returncode == (0 if success else 1), output
        assert {name: (self.consumer / name).read_bytes() for name in before} == before
        return output


def _scenario(
    tmp_path: Path,
    apm_binary_path: Path,
    *,
    plugin_mcp: bool = True,
    root_mcp: bool = True,
    transitive: bool = False,
    shape: str = "plugin",
    subdirectory: bool = False,
) -> _Scenario:
    """Generate all baseline state through the CLI; author only package inputs."""
    producer_env = IsolatedApmEnvironment.create(tmp_path / "producer", base_env=dict(os.environ))
    consumer_env = IsolatedApmEnvironment.create(tmp_path / "consumer", base_env=dict(os.environ))
    repository_source = producer_env.package_root / "plugin"
    source = repository_source / "plugins/nested" if subdirectory else repository_source
    source.mkdir(parents=True)
    if shape == "plugin":
        metadata = source / ".claude-plugin"
        metadata.mkdir()
        (metadata / "plugin.json").write_text(
            json.dumps({"name": "frozen-plugin", "version": "1.0.0"}),
            encoding="utf-8",
        )
    skill = (
        source / "SKILL.md"
        if shape in {"hybrid", "claude_skill"}
        else source / "skills" / "frozen-skill" / "SKILL.md"
    )
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_bytes(_SKILL)
    if shape == "plugin" and plugin_mcp:
        (source / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"plugin-server": _PLUGIN_MCP}}), encoding="utf-8"
        )
    if shape in {"hybrid", "apm_package"}:
        if shape == "apm_package":
            skill.unlink()
            skill.parent.rmdir()
            skill.parent.parent.rmdir()
            primitive = source / ".apm/instructions/frozen.instructions.md"
            primitive.parent.mkdir(parents=True)
            primitive.write_text(
                "---\napplyTo: '**'\n---\n# Frozen instruction\n", encoding="utf-8"
            )
        dump_yaml(
            {
                "name": "frozen-plugin",
                "version": "1.0.0",
                "dependencies": {
                    "mcp": [
                        {
                            "name": "plugin-server",
                            "transport": "stdio",
                            "registry": False,
                            **_PLUGIN_MCP,
                        }
                    ]
                }
                if plugin_mcp
                else {},
            },
            source / "apm.yml",
        )
    repositories = LocalGitRepositoryFactory(
        producer_env.repository_root, env=producer_env.subprocess_env()
    )
    repository = repositories.create("plugin", source_tree=repository_source)
    commit = repositories.commit(repository, message="Pinned whole plugin")
    dependency = {"git": _REMOTE, "type": "gitlab", "ref": commit.sha}
    if subdirectory:
        dependency["path"] = "plugins/nested"
    pins = {commit.sha}
    rewrites = [(repository, _REMOTE)]
    if transitive:
        wrapper = LocalPackageFactory(producer_env.package_root).create(
            "wrapper", dependencies=(dependency,)
        )
        wrapper_repo = repositories.create("wrapper", source_tree=wrapper.root)
        wrapper_commit = repositories.commit(wrapper_repo, message="Pinned transitive edge")
        wrapper_url = "https://gitlab.example.invalid/frozen/wrapper.git"
        dependency = {"git": wrapper_url, "type": "gitlab", "ref": wrapper_commit.sha}
        pins.add(wrapper_commit.sha)
        rewrites.append((wrapper_repo, wrapper_url))
    transport_env = repositories.url_rewrite_subprocess_env_many(rewrites)
    producer = (
        LocalPackageFactory(producer_env.work_root)
        .create(
            "project",
            dependencies=(dependency,),
            mcp_dependencies=(_ROOT_MCP,) if root_mcp else (),
            targets=("copilot",),
        )
        .root
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    runner.run_sequence(
        (("install", "--no-policy", "--parallel-downloads", "0"),),
        expected_returncodes=(0,),
        scenario_id="whole-plugin-producer",
        cwd=producer,
        env=transport_env,
    )
    manifest_bytes = (producer / "apm.yml").read_bytes()
    lock_bytes = (producer / "apm.lock.yaml").read_bytes()
    lock = load_yaml(producer / "apm.lock.yaml")
    assert {entry["resolved_commit"] for entry in lock["dependencies"]} == pins
    assert {entry["resolved_ref"] for entry in lock["dependencies"]} == pins
    expected_type = {"plugin": "skill_bundle", "skill_bundle": "skill_bundle"}.get(shape, shape)
    assert (
        next(entry for entry in lock["dependencies"] if entry["repo_url"] == "frozen/plugin")[
            "package_type"
        ]
        == expected_type
    )
    expected_names = ({"root-server"} if root_mcp else set()) | (
        {"plugin-server"} if plugin_mcp and not transitive else set()
    )
    assert set(lock.get("mcp_servers", [])) == expected_names

    consumer = consumer_env.work_root / "project"
    consumer.mkdir()
    (consumer / "apm.yml").write_bytes(manifest_bytes)
    (consumer / "apm.lock.yaml").write_bytes(lock_bytes)
    child_env = consumer_env.subprocess_env()
    child_env.update(
        {
            key: value
            for key, value in transport_env.items()
            if key == "GIT_CONFIG_COUNT" or key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
        }
    )
    assert not (consumer / "apm_modules").exists()
    return _Scenario(
        runner,
        producer,
        consumer,
        child_env,
        transport_env,
        repositories,
        repository,
        pins,
        expected_type,
    )


@pytest.mark.parametrize("plugin_mcp", [False, True])
@pytest.mark.parametrize("root_mcp", [False, True])
@pytest.mark.parametrize("transitive", [False, True])
def test_frozen_whole_plugin_cold_consumer(
    tmp_path: Path,
    apm_binary_path: Path,
    plugin_mcp: bool,
    root_mcp: bool,
    transitive: bool,
) -> None:
    """Restore exact pins and bytes, without granting transitive executable trust."""
    scenario = _scenario(
        tmp_path,
        apm_binary_path,
        plugin_mcp=plugin_mcp,
        root_mcp=root_mcp,
        transitive=transitive,
    )
    for _ in range(2):
        scenario.freeze()
        assert (scenario.consumer / ".agents/skills/frozen-skill/SKILL.md").read_bytes() == _SKILL
        restored = scenario.consumer / "apm_modules/frozen/plugin"
        assert (restored / ".claude-plugin/plugin.json").read_bytes() == (
            scenario.repository.worktree / ".claude-plugin/plugin.json"
        ).read_bytes()
        _assert_mcp_matches_producer(scenario)


def test_frozen_whole_plugin_subdirectory(tmp_path: Path, apm_binary_path: Path) -> None:
    """Replay a producer-selected whole plugin without losing its virtual source path."""
    scenario = _scenario(tmp_path, apm_binary_path, subdirectory=True)
    lock = LockFile.read(scenario.consumer / "apm.lock.yaml")
    dependency = next(iter(lock.dependencies.values()))
    assert dependency.virtual_path == "plugins/nested"
    assert dependency.resolved_commit in scenario.pins
    restored = dependency.to_dependency_ref().get_install_path(scenario.consumer / "apm_modules")
    for _ in range(2):
        scenario.freeze()
        assert (scenario.consumer / ".agents/skills/frozen-skill/SKILL.md").read_bytes() == _SKILL
        assert (restored / ".claude-plugin/plugin.json").read_bytes() == (
            scenario.repository.worktree / "plugins/nested/.claude-plugin/plugin.json"
        ).read_bytes()
        _assert_mcp_matches_producer(scenario)


@pytest.mark.parametrize("plugin_mcp", [False, True])
def test_frozen_cold_consumer_tolerates_absent_no_mcp_orphan(
    tmp_path: Path, apm_binary_path: Path, plugin_mcp: bool
) -> None:
    """An unused producer-locked package must not require hydration or MCP evidence."""
    scenario = _scenario(tmp_path, apm_binary_path, plugin_mcp=plugin_mcp, root_mcp=plugin_mcp)
    packages = LocalPackageFactory(tmp_path / "orphan-source")
    orphan = packages.create("unused")
    packages.add_instruction(orphan, "unused", "---\napplyTo: '**'\n---\n# Unused instruction\n")
    repository = scenario.repositories.create("unused", source_tree=orphan.root)
    commit = scenario.repositories.commit(repository, message="Unused package without MCP")
    remote = "https://gitlab.example.invalid/frozen/unused.git"
    transport = scenario.repositories.url_rewrite_subprocess_env_many(
        [(scenario.repository, _REMOTE), (repository, remote)]
    )
    scenario.producer_environment.update(transport)
    for key, value in transport.items():
        if key == "GIT_CONFIG_COUNT" or key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            scenario.environment[key] = value
    manifest = load_yaml(scenario.producer / "apm.yml")
    original_dependencies = list(manifest["dependencies"]["apm"])
    manifest["dependencies"]["apm"].append({"git": remote, "type": "gitlab", "ref": commit.sha})
    dump_yaml(manifest, scenario.producer / "apm.yml")
    scenario.runner.run_sequence(
        (("install", "--no-policy", "--parallel-downloads", "0"),),
        expected_returncodes=(0,),
        scenario_id="producer-with-unused-lock-entry",
        cwd=scenario.producer,
        env=scenario.producer_environment,
    )
    lock = load_yaml(scenario.producer / "apm.lock.yaml")
    assert {entry["resolved_commit"] for entry in lock["dependencies"]} == {
        *scenario.pins,
        commit.sha,
    }
    manifest["dependencies"]["apm"] = original_dependencies
    dump_yaml(manifest, scenario.consumer / "apm.yml")
    (scenario.consumer / "apm.lock.yaml").write_bytes(
        (scenario.producer / "apm.lock.yaml").read_bytes()
    )
    for _ in range(2):
        scenario.freeze()
        assert (scenario.consumer / ".agents/skills/frozen-skill/SKILL.md").read_bytes() == _SKILL
        assert not (scenario.consumer / "apm_modules/frozen/unused").exists()
        _assert_mcp_matches_producer(scenario)


def test_frozen_replays_pin_after_remote_head_advances(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Source bytes must come from the locked commit, not the repository's newer head."""
    scenario = _scenario(tmp_path, apm_binary_path)
    source_skill = scenario.repository.worktree / "skills/frozen-skill/SKILL.md"
    source_skill.write_bytes(_SKILL + b"\nUnselected repository head.\n")
    newer = scenario.repositories.commit(scenario.repository, message="Unselected newer head")
    assert newer.sha not in scenario.pins
    for _ in range(2):
        scenario.freeze()
        assert (scenario.consumer / ".agents/skills/frozen-skill/SKILL.md").read_bytes() == _SKILL
        assert {
            entry["resolved_commit"]
            for entry in load_yaml(scenario.consumer / "apm.lock.yaml")["dependencies"]
        } == scenario.pins
        _assert_mcp_matches_producer(scenario)


def _assert_mcp_matches_producer(scenario: _Scenario) -> None:
    """Compare actual native config, not merely the lockfile's intended server set."""
    relative = Path(".github/mcp.json")
    baseline = scenario.producer / relative
    actual = scenario.consumer / relative
    if baseline.exists():
        assert json.loads(actual.read_text(encoding="utf-8")) == json.loads(
            baseline.read_text(encoding="utf-8")
        )
    else:
        assert not actual.exists()


@pytest.mark.parametrize("shape", ["apm_package", "hybrid", "claude_skill", "skill_bundle"])
def test_frozen_related_package_shapes(tmp_path: Path, apm_binary_path: Path, shape: str) -> None:
    """Related supported source shapes retain pinned cold and repeated replay."""
    scenario = _scenario(
        tmp_path, apm_binary_path, shape=shape, plugin_mcp=shape in {"apm_package", "hybrid"}
    )
    for _ in range(2):
        scenario.freeze()
        _assert_mcp_matches_producer(scenario)
        assert {
            entry["resolved_commit"]
            for entry in load_yaml(scenario.consumer / "apm.lock.yaml")["dependencies"]
        } == scenario.pins
        restored = scenario.consumer / "apm_modules/frozen/plugin"
        for path in scenario.repository.worktree.rglob("*"):
            if (
                path.is_file()
                and ".git" not in path.relative_to(scenario.repository.worktree).parts
            ):
                assert (
                    restored / path.relative_to(scenario.repository.worktree)
                ).read_bytes() == path.read_bytes()


@pytest.mark.parametrize("change", ["changed", "removed", "added"])
def test_frozen_hydrated_mcp_drift_preserves_native_settings(
    tmp_path: Path, apm_binary_path: Path, change: str
) -> None:
    """Newly deferred drift must fail before replacing user or prior MCP configuration."""
    scenario = _scenario(
        tmp_path, apm_binary_path, plugin_mcp=change != "added", root_mcp=change != "added"
    )
    old_lock = load_yaml(scenario.producer / "apm.lock.yaml")
    source_config = scenario.repository.worktree / ".mcp.json"
    if change == "removed":
        source_config.unlink()
    else:
        source_config.write_text(
            json.dumps({"mcpServers": {"plugin-server": {"command": "changed-command"}}}),
            encoding="utf-8",
        )
    changed_commit = scenario.repositories.commit(scenario.repository, message=f"MCP {change}")
    manifest = load_yaml(scenario.producer / "apm.yml")
    manifest["dependencies"]["apm"][0]["ref"] = changed_commit.sha
    dump_yaml(manifest, scenario.producer / "apm.yml")
    scenario.runner.run_sequence(
        (("install", "--no-policy", "--parallel-downloads", "0"),),
        expected_returncodes=(0,),
        scenario_id="changed-source-producer",
        cwd=scenario.producer,
        env=scenario.producer_environment,
    )
    changed_lock = load_yaml(scenario.producer / "apm.lock.yaml")
    assert changed_lock["dependencies"][0]["resolved_commit"] == changed_commit.sha
    for field in ("mcp_configs", "mcp_servers", "mcp_config_provenance"):
        changed_lock[field] = old_lock.get(field, [] if field == "mcp_servers" else {})
    dump_yaml(changed_lock, scenario.consumer / "apm.lock.yaml")
    (scenario.consumer / "apm.yml").write_bytes((scenario.producer / "apm.yml").read_bytes())
    native = scenario.consumer / ".github/mcp.json"
    native.parent.mkdir()
    native.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "plugin-server": {"command": "previous"},
                    "user-owned": {"command": "keep"},
                },
                "userSetting": True,
            }
        ),
        encoding="utf-8",
    )
    before = native.read_bytes()
    output = scenario.freeze(success=False)
    assert "restored MCP declarations differ" in output
    assert "plugin-server" in output
    assert native.read_bytes() == before
    assert not (scenario.consumer / ".agents/skills/frozen-skill").exists()


@pytest.mark.parametrize(
    "problem",
    [
        "missing-pin",
        "incompatible-pin",
        "unknown-shape",
        "ambiguous-owner",
        "shared-owner",
        "unavailable-source",
    ],
)
def test_frozen_refuses_unverifiable_source_evidence(
    tmp_path: Path, apm_binary_path: Path, problem: str
) -> None:
    """Real producer evidence is not permission to accept malformed or unavailable replay."""
    scenario = _scenario(tmp_path, apm_binary_path)
    lock = load_yaml(scenario.consumer / "apm.lock.yaml")
    if problem == "missing-pin":
        lock["dependencies"][0].pop("resolved_commit")
    elif problem == "incompatible-pin":
        lock["dependencies"][0]["resolved_commit"] = "f" * 40
    elif problem == "unknown-shape":
        lock["dependencies"][0]["package_type"] = "not-a-supported-package"
    elif problem == "ambiguous-owner":
        lock["dependencies"][0].pop("name")
    elif problem == "shared-owner":
        lock["mcp_config_provenance"]["plugin-server"] = [
            "frozen-plugin",
            "unavailable-other-owner",
        ]
    else:
        scenario.repository.origin.rename(scenario.repository.origin.with_name("offline.git"))
    dump_yaml(lock, scenario.consumer / "apm.lock.yaml")
    native = scenario.consumer / ".github/mcp.json"
    native.parent.mkdir()
    native.write_text(
        '{"mcpServers":{"user-owned":{"command":"keep"}},"userSetting":true}\n', encoding="utf-8"
    )
    previous = native.read_bytes()
    output = scenario.freeze(success=False)
    assert "[x]" in output
    assert native.read_bytes() == previous
    assert not (scenario.consumer / ".agents/skills/frozen-skill").exists()


def test_frozen_root_declaration_keeps_precedence(tmp_path: Path, apm_binary_path: Path) -> None:
    """A root declaration wins over an identically named hydrated plugin declaration."""
    scenario = _scenario(tmp_path, apm_binary_path)
    manifest = load_yaml(scenario.producer / "apm.yml")
    manifest["dependencies"]["mcp"].append(
        {**_ROOT_MCP, "name": "plugin-server", "command": "root-winner"}
    )
    dump_yaml(manifest, scenario.producer / "apm.yml")
    scenario.runner.run_sequence(
        (("install", "--no-policy"),),
        expected_returncodes=(0,),
        scenario_id="root-precedence-producer",
        cwd=scenario.producer,
        env=scenario.producer_environment,
    )
    for name in ("apm.yml", "apm.lock.yaml"):
        (scenario.consumer / name).write_bytes((scenario.producer / name).read_bytes())
    lock = load_yaml(scenario.consumer / "apm.lock.yaml")
    assert lock["mcp_configs"]["plugin-server"]["command"] == "root-winner"
    assert "plugin-server" not in lock.get("mcp_config_provenance", {})
    for _ in range(2):
        scenario.freeze()
        _assert_mcp_matches_producer(scenario)


@pytest.mark.parametrize("problem", ["invalid-shape", "present-mcp-mismatch", "missing-local"])
def test_frozen_does_not_defer_present_or_local_sources(
    tmp_path: Path, apm_binary_path: Path, problem: str
) -> None:
    """Cold remote restoration cannot waive current-source or local-source safeguards."""
    scenario = _scenario(tmp_path, apm_binary_path)
    if problem == "missing-local":
        local = LocalPackageFactory(scenario.producer).create("local")
        manifest = load_yaml(scenario.producer / "apm.yml")
        manifest["dependencies"]["apm"].append({"path": "./local"})
        dump_yaml(manifest, scenario.producer / "apm.yml")
        scenario.runner.run_sequence(
            (("install", "--no-policy"),),
            expected_returncodes=(0,),
            scenario_id="local-source-producer",
            cwd=scenario.producer,
            env=scenario.producer_environment,
        )
        assert local.root.is_dir()
        for name in ("apm.yml", "apm.lock.yaml"):
            (scenario.consumer / name).write_bytes((scenario.producer / name).read_bytes())
        assert not (scenario.consumer / "local").exists()
    else:
        present = scenario.consumer / "apm_modules/frozen/plugin"
        present.mkdir(parents=True)
        if problem == "present-mcp-mismatch":
            dump_yaml(
                {
                    "name": "frozen-plugin",
                    "version": "1.0.0",
                    "dependencies": {
                        "mcp": [{**_ROOT_MCP, "name": "plugin-server", "command": "changed"}]
                    },
                },
                present / "apm.yml",
            )
    native = scenario.consumer / ".github/mcp.json"
    native.parent.mkdir()
    native.write_text('{"mcpServers":{"user-owned":{"command":"keep"}}}\n', encoding="utf-8")
    previous = native.read_bytes()
    output = scenario.freeze(success=False)
    assert "--frozen" in output
    assert native.read_bytes() == previous
    assert not (scenario.consumer / ".agents/skills/frozen-skill").exists()


def test_frozen_services_only_cannot_defer_without_hydration(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A services-only invocation cannot accept missing package declarations without fetching."""
    scenario = _scenario(tmp_path, apm_binary_path)
    native = scenario.consumer / ".github/mcp.json"
    native.parent.mkdir()
    native.write_text('{"mcpServers":{"user-owned":{"command":"keep"}}}\n', encoding="utf-8")
    previous = native.read_bytes()
    output = scenario.freeze(success=False, only_mcp=True)
    assert "--frozen" in output
    assert native.read_bytes() == previous
    assert not (scenario.consumer / "apm_modules").exists()
