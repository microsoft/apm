"""Real-binary Agent Plugin ownership, state, failure, and trust contracts."""

from __future__ import annotations

import json
import os
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "agent_plugins" / "portable"
_PLUGIN_NAME = "contract-plugin"
_PLUGIN_VERSION = "1.0.0"
_APPROVAL_KEY = f"{_PLUGIN_NAME}#{_PLUGIN_VERSION}"
_KIRO_CONFIG = PurePosixPath(".kiro/settings/mcp.json")
_SKILL_PATH = PurePosixPath(".kiro/skills/contract-skill/SKILL.md")
_AUDIT_ARGS = (
    "audit",
    "--ci",
    "--no-policy",
    "--format",
    "json",
    "--output",
    "reports/audit.json",
)

_APM_REQUIREMENTS = {
    "identity": "APM-PLUGIN-LC-1 manifest identity owns retained root, data, and services",
    "state": "APM-PLUGIN-LC-2 install/update/audit/uninstall converge durable state",
    "invalid": "APM-PLUGIN-LC-3 invalid executable components cannot report full success",
    "projection": "APM-PLUGIN-LC-4 portable expressions project without target reinterpretation",
    "ambient": "APM-PLUGIN-SC-1 ambient credentials never reach plugin-native config",
    "trust": "APM-PLUGIN-SC-2 executable denial is source-form invariant",
}


@dataclass(frozen=True)
class _RuntimeState:
    """Exact lifecycle state plus retained Agent Plugin root names."""

    snapshot: LifecycleStateSnapshot
    retained_roots: tuple[str, ...]
    data_roots: tuple[str, ...]
    retained_files: tuple[tuple[str, bytes], ...]
    data_files: tuple[tuple[str, bytes], ...]


def _copy_plugin(destination: Path) -> Path:
    shutil.copytree(_FIXTURE, destination)
    (destination / "mcp.json").write_text(
        json.dumps(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
                "mcpServers": {
                    "contract-stdio": {
                        "type": "stdio",
                        "command": "printf",
                        "args": ["contract"],
                    }
                },
            },
            indent=2,
        )
        + "\n",
        encoding="ascii",
    )
    return destination


def _write_project(
    root: Path,
    *,
    target: str,
    approve: bool,
    dependencies: tuple[dict[str, object], ...] = (),
) -> Path:
    root.mkdir(parents=True)
    manifest: dict[str, object] = {
        "name": f"{target}-contract-consumer",
        "version": "0.1.0",
        "description": "Agent Plugin lifecycle contract consumer",
        "targets": [target],
    }
    if dependencies:
        manifest["dependencies"] = {"apm": list(dependencies)}
    manifest["allowExecutables"] = {_APPROVAL_KEY: {"mcp": True, "lsp": True}} if approve else {}
    dump_yaml(manifest, root / "apm.yml")
    if target == "vscode":
        (root / ".vscode").mkdir()
    elif target == "kiro":
        (root / ".kiro" / "settings").mkdir(parents=True)
    return root


def _runner(binary: Path) -> ApmLifecycleRunner:
    return ApmLifecycleRunner(
        (str(binary),),
        timeout_seconds=120,
        scenario_timeout_seconds=300,
    )


def _run(
    runner: ApmLifecycleRunner,
    project: Path,
    environment: dict[str, str],
    args: tuple[str, ...],
    scenario_id: str,
) -> CommandResult:
    return runner.run(args, scenario_id=scenario_id, cwd=project, env=environment)


def _assert_success(result: CommandResult) -> None:
    assert result.returncode == 0, (
        f"command={result.command!r}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _archive_plugin(source: Path, destination: Path) -> Path:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(source).as_posix())
    return destination


def _runtime_state(project: Path, config_path: PurePosixPath) -> _RuntimeState:
    retained_parent = project / "apm_modules" / ".agent-plugins"
    data_parent = project / "apm_modules" / ".plugin-data"
    return _RuntimeState(
        snapshot=LifecycleStateSnapshot.capture(
            project,
            config_paths=(config_path,),
        ),
        retained_roots=tuple(
            sorted(path.name for path in retained_parent.iterdir() if path.is_dir())
        )
        if retained_parent.is_dir()
        else (),
        data_roots=tuple(sorted(path.name for path in data_parent.iterdir() if path.is_dir()))
        if data_parent.is_dir()
        else (),
        retained_files=_tree_files(retained_parent),
        data_files=_tree_files(data_parent),
    )


def _tree_files(root: Path) -> tuple[tuple[str, bytes], ...]:
    if not root.is_dir():
        return ()
    return tuple(
        (path.relative_to(root).as_posix(), path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )


def _lock_service_owners(project: Path) -> tuple[str, ...]:
    lock_path = project / "apm.lock.yaml"
    if not lock_path.is_file():
        return ()
    lock = load_yaml(lock_path)
    owners = {
        str(owner)
        for field in ("mcp_config_provenance", "lsp_config_provenance")
        for owner in (lock.get(field) or {}).values()
    }
    return tuple(sorted(owners))


def _config_text(project: Path, path: PurePosixPath) -> str:
    config = project.joinpath(*path.parts)
    return config.read_text(encoding="utf-8") if config.is_file() else ""


def _resolved_text(path: Path) -> str:
    """Return the canonical text used by runtime-owned filesystem values."""
    return str(path.resolve())


def _target_mcp_servers(state: _RuntimeState) -> tuple[str, ...]:
    config = state.snapshot.file(_KIRO_CONFIG.as_posix())
    if config.kind != "file" or config.content is None:
        return ()
    document = json.loads(config.content)
    return tuple(sorted((document.get("mcpServers") or {}).keys()))


def test_archive_filename_does_not_change_installed_plugin_owner(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """APM-PLUGIN-LC-1 binds identity to plugin.json, not archive path."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "identity", base_env=os.environ)
    environment = isolated.subprocess_env()
    project = _write_project(
        isolated.work_root / "consumer",
        target="kiro",
        approve=True,
    )
    source = _copy_plugin(isolated.package_root / "source")
    (source / "mcp.json").unlink()
    first_archive = _archive_plugin(source, isolated.package_root / "first-name.zip")
    second_archive = _archive_plugin(source, isolated.package_root / "renamed-copy.zip")
    runner = _runner(apm_binary_path)

    first_install = _run(
        runner,
        project,
        environment,
        ("install", str(first_archive), "--target", "kiro", "--no-policy"),
        "agent-plugin-identity-first",
    )
    _assert_success(first_install)
    first = _runtime_state(project, _KIRO_CONFIG)
    first_owners = _lock_service_owners(project)
    assert len(first.retained_roots) == 1
    assert len(first.data_roots) == 1
    assert len(first_owners) == 1
    data_sentinel = (
        project / "apm_modules" / ".plugin-data" / first.data_roots[0] / "persisted-state.txt"
    )
    data_sentinel.write_text("preserve across archive rename\n", encoding="ascii")
    first_with_data = _runtime_state(project, _KIRO_CONFIG)

    renamed_install = _run(
        runner,
        project,
        environment,
        ("install", str(second_archive), "--target", "kiro", "--no-policy"),
        "agent-plugin-identity-renamed",
    )
    renamed = _runtime_state(project, _KIRO_CONFIG)
    renamed_owners = _lock_service_owners(project)

    assert renamed_install.returncode == 0, (
        f"{_APM_REQUIREMENTS['identity']}\n"
        f"first_owner={first_owners!r}\nrenamed_owner={renamed_owners!r}\n"
        f"first_roots={first.retained_roots!r}\nrenamed_roots={renamed.retained_roots!r}\n"
        f"first_data={first.data_roots!r}\nrenamed_data={renamed.data_roots!r}\n"
        f"stdout={renamed_install.stdout!r}\nstderr={renamed_install.stderr!r}"
    )
    assert renamed.retained_roots == first.retained_roots
    assert renamed.data_roots == first.data_roots
    assert renamed_owners == first_owners
    assert renamed.retained_files == first.retained_files
    assert renamed.data_files == first_with_data.data_files
    assert data_sentinel.read_text(encoding="ascii") == "preserve across archive rename\n"


def test_public_plugin_lifecycle_snapshots_every_durable_transition(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """APM-PLUGIN-LC-2 covers lock, ledger, roots, bytes, config, and cleanup."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "lifecycle", base_env=os.environ)
    environment = isolated.subprocess_env()
    project = _write_project(
        isolated.work_root / "consumer",
        target="kiro",
        approve=True,
    )
    source = _copy_plugin(isolated.package_root / "source")
    runner = _runner(apm_binary_path)
    install_args = ("install", str(source), "--target", "kiro", "--no-policy")

    installed_result = _run(
        runner,
        project,
        environment,
        install_args,
        "agent-plugin-lifecycle-install",
    )
    assert installed_result.returncode == 0, (
        f"{_APM_REQUIREMENTS['state']}\n"
        f"stdout={installed_result.stdout!r}\nstderr={installed_result.stderr!r}"
    )
    installed = _runtime_state(project, _KIRO_CONFIG)
    assert installed.snapshot.lockfile_bytes is not None
    assert installed.snapshot.file(_SKILL_PATH.as_posix()).kind == "file"
    assert installed.snapshot.file(_KIRO_CONFIG.as_posix()).kind == "file"
    assert _target_mcp_servers(installed) == ("contract-stdio",)
    assert len(installed.retained_roots) == 1
    assert len(installed.data_roots) == 1

    data_sentinel = project / "apm_modules" / ".plugin-data" / installed.data_roots[0] / "state.txt"
    data_sentinel.write_text("persistent\n", encoding="ascii")
    installed_with_data = _runtime_state(project, _KIRO_CONFIG)
    source_skill = source / "skills" / "contract-skill" / "SKILL.md"
    source_skill.write_text(
        source_skill.read_text(encoding="utf-8").replace(
            "Initial fixture bytes.",
            "Updated fixture bytes.",
        ),
        encoding="utf-8",
    )
    reinstalled_result = _run(
        runner,
        project,
        environment,
        install_args,
        "agent-plugin-lifecycle-reinstall",
    )
    _assert_success(reinstalled_result)
    reinstalled = _runtime_state(project, _KIRO_CONFIG)
    assert b"Updated fixture bytes." in reinstalled.snapshot.file(_SKILL_PATH.as_posix()).content
    assert reinstalled.retained_roots == installed_with_data.retained_roots
    assert reinstalled.data_roots == installed_with_data.data_roots
    assert reinstalled.data_files == installed_with_data.data_files
    assert _target_mcp_servers(reinstalled) == ("contract-stdio",)

    updated_result = _run(
        runner,
        project,
        environment,
        ("update", "--yes", "--target", "kiro"),
        "agent-plugin-lifecycle-update",
    )
    updated = _runtime_state(project, _KIRO_CONFIG)
    (project / "reports").mkdir()
    audit_result = _run(
        runner,
        project,
        environment,
        _AUDIT_ARGS,
        "agent-plugin-lifecycle-audit",
    )
    audited = _runtime_state(project, _KIRO_CONFIG)
    uninstalled_result = _run(
        runner,
        project,
        environment,
        ("uninstall", _PLUGIN_NAME),
        "agent-plugin-lifecycle-uninstall",
    )
    uninstalled = _runtime_state(project, _KIRO_CONFIG)

    violations: list[str] = []
    if updated_result.returncode != 0:
        violations.append(
            f"update returned {updated_result.returncode}: "
            f"{updated_result.stdout}{updated_result.stderr}"
        )
    if updated.snapshot.semantic_bytes != reinstalled.snapshot.semantic_bytes:
        violations.append("update changed installed plugin durable semantics without new input")
    if updated.retained_roots != reinstalled.retained_roots:
        violations.append("update changed the retained plugin root")
    if updated.retained_files != reinstalled.retained_files:
        violations.append("update changed retained plugin bytes without new input")
    if updated.data_roots != reinstalled.data_roots:
        violations.append("update lost the persistent PLUGIN_DATA root")
    if updated.data_files != reinstalled.data_files:
        violations.append("update changed persistent PLUGIN_DATA bytes")
    if _target_mcp_servers(updated) != ("contract-stdio",):
        violations.append("update silently dropped the target MCP server")
    if audit_result.returncode != 0:
        violations.append(
            f"audit returned {audit_result.returncode}: {audit_result.stdout}{audit_result.stderr}"
        )
    if audited.snapshot.semantic_bytes != updated.snapshot.semantic_bytes:
        violations.append("audit mutated installed plugin durable semantics")
    if audited.retained_files != updated.retained_files:
        violations.append("audit mutated retained plugin bytes")
    if audited.data_files != updated.data_files:
        violations.append("audit mutated persistent PLUGIN_DATA bytes")
    if audited.retained_roots != updated.retained_roots:
        violations.append("audit changed the retained plugin root")
    if audited.data_roots != updated.data_roots:
        violations.append("audit changed the persistent PLUGIN_DATA root")
    if _target_mcp_servers(audited) != ("contract-stdio",):
        violations.append("audit silently dropped the target MCP server")
    if uninstalled_result.returncode != 0:
        violations.append(
            f"uninstall returned {uninstalled_result.returncode}: "
            f"{uninstalled_result.stdout}{uninstalled_result.stderr}"
        )
    if uninstalled.retained_roots:
        violations.append(f"uninstall retained plugin roots: {uninstalled.retained_roots!r}")
    if uninstalled.data_roots:
        violations.append(f"uninstall retained plugin data roots: {uninstalled.data_roots!r}")
    if uninstalled.snapshot.file(_SKILL_PATH.as_posix()).kind != "missing":
        violations.append("uninstall retained deployed skill bytes")
    if b"contract-stdio" in (uninstalled.snapshot.file(_KIRO_CONFIG.as_posix()).content or b""):
        violations.append("uninstall retained the plugin server in target MCP config")
    assert violations == [], _APM_REQUIREMENTS["state"] + "\n" + "\n".join(violations)


def test_invalid_plugin_services_cannot_create_false_success_state(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """ss7.2.2/ss11.3 keep the skill, while APM-PLUGIN-LC-3 reports partial failure."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "invalid", base_env=os.environ)
    environment = isolated.subprocess_env()
    project = _write_project(
        isolated.work_root / "consumer",
        target="kiro",
        approve=True,
    )
    source = _copy_plugin(isolated.package_root / "invalid-services")
    (source / "mcp.json").write_text(
        json.dumps(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
                "mcpServers": {
                    "invalid-mcp": {
                        "type": "stdio",
                        "command": "sh -c forbidden",
                    }
                },
            },
            indent=2,
        )
        + "\n",
        encoding="ascii",
    )
    (source / "com.microsoft.apm" / "lsp.json").write_text(
        json.dumps(
            {
                "lspServers": {
                    "invalid-lsp": {
                        "command": "sh -c forbidden",
                        "extensionToLanguage": {".txt": "text"},
                    }
                }
            },
            indent=2,
        )
        + "\n",
        encoding="ascii",
    )

    result = _run(
        _runner(apm_binary_path),
        project,
        environment,
        ("install", str(source), "--target", "kiro", "--no-policy"),
        "agent-plugin-invalid-services",
    )
    state = _runtime_state(project, _KIRO_CONFIG)
    assert state.snapshot.file(_SKILL_PATH.as_posix()).kind == "file"
    assert state.snapshot.mcp_state_bytes == (
        b'{"configs":{},"provenance":{},"servers":[],"target_servers":{}}'
    )
    assert state.snapshot.lsp_state_bytes == b'{"configs":{},"servers":[]}'
    assert "invalid-mcp" not in _config_text(project, _KIRO_CONFIG)
    assert result.returncode != 0, (
        f"{_APM_REQUIREMENTS['invalid']}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}\n"
        f"lockfile_present={state.snapshot.lockfile_bytes is not None}"
    )


def test_runtime_projection_expands_only_portable_path_fields(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """APM-PLUGIN-LC-4 projects ss9.2 paths while preserving ss7.2.1 literals."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "projection", base_env=os.environ)
    environment = isolated.subprocess_env()
    project = _write_project(
        isolated.work_root / "consumer",
        target="kiro",
        approve=True,
    )
    source = _copy_plugin(isolated.package_root / "projection-plugin")
    (source / "mcp.json").write_text(
        json.dumps(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
                "mcpServers": {
                    "ambient-probe": {
                        "type": "stdio",
                        "command": "printf",
                        "args": [
                            "${PLUGIN_ROOT}/bin/tool",
                            "${PLUGIN_DATA}/state",
                            "${UNKNOWN_VAR}",
                        ],
                        "env": {
                            "ROOT_REF": "${PLUGIN_ROOT}/config",
                            "DATA_REF": "${PLUGIN_DATA}/cache",
                            "UNKNOWN_REF": "${UNKNOWN_VAR}",
                        },
                        "cwd": "${PLUGIN_ROOT}",
                    },
                    "literal-remote": {
                        "type": "streamable-http",
                        "url": "https://example.invalid/${PLUGIN_ROOT}/mcp",
                        "headers": {
                            "X-Plugin-Data": "${PLUGIN_DATA}",
                            "X-Unknown": "${UNKNOWN_VAR}",
                        },
                    },
                },
            },
            indent=2,
        )
        + "\n",
        encoding="ascii",
    )

    result = _run(
        _runner(apm_binary_path),
        project,
        environment,
        ("install", str(source), "--target", "kiro", "--no-policy"),
        "agent-plugin-runtime-projection",
    )
    config_text = _config_text(project, _KIRO_CONFIG)
    violations: list[str] = []
    if result.returncode != 0:
        violations.append(f"install returned {result.returncode}")
    try:
        config = json.loads(config_text)
    except json.JSONDecodeError as exc:
        config = {}
        violations.append(f"target MCP config is not valid JSON: {exc}")
    servers = config.get("mcpServers") if isinstance(config, dict) else None
    if not isinstance(servers, dict):
        servers = {}
        violations.append("target MCP config has no mcpServers object")
    stdio = servers.get("ambient-probe")
    remote = servers.get("literal-remote")
    if not isinstance(stdio, dict):
        violations.append("target MCP config omitted ambient-probe")
    else:
        projected_env = stdio.get("env")
        if not isinstance(projected_env, dict):
            violations.append("ambient-probe has no env object")
        else:
            root_value = projected_env.get("PLUGIN_ROOT")
            data_value = projected_env.get("PLUGIN_DATA")
            if not isinstance(root_value, str) or not isinstance(data_value, str):
                violations.append("reserved runtime paths were not projected as strings")
            else:
                projected_root = Path(root_value)
                projected_data = Path(data_value)
                if projected_root.name != projected_data.name:
                    violations.append("PLUGIN_ROOT and PLUGIN_DATA use different owner identities")
                if root_value != _resolved_text(
                    project / "apm_modules" / ".agent-plugins" / projected_root.name
                ):
                    violations.append("PLUGIN_ROOT was not projected to the resolved retained root")
                if data_value != _resolved_text(
                    project / "apm_modules" / ".plugin-data" / projected_data.name
                ):
                    violations.append("PLUGIN_DATA was not projected to the resolved data root")
                if stdio.get("args") != [
                    f"{root_value}/bin/tool",
                    f"{data_value}/state",
                    "${UNKNOWN_VAR}",
                ]:
                    violations.append("args did not expand only PLUGIN_ROOT and PLUGIN_DATA")
                if projected_env.get("ROOT_REF") != f"{root_value}/config":
                    violations.append(
                        "env ROOT_REF was reinterpreted instead of expanding PLUGIN_ROOT"
                    )
                if projected_env.get("DATA_REF") != f"{data_value}/cache":
                    violations.append(
                        "env DATA_REF was reinterpreted instead of expanding PLUGIN_DATA"
                    )
                if stdio.get("cwd") != root_value:
                    violations.append("cwd did not expand PLUGIN_ROOT")
            if projected_env.get("UNKNOWN_REF") != "${UNKNOWN_VAR}":
                violations.append("unknown env expression was not preserved literally")
    if not isinstance(remote, dict):
        violations.append("target MCP config omitted literal-remote")
    else:
        parsed_url = urlparse(str(remote.get("url", "")))
        if (
            parsed_url.scheme,
            parsed_url.hostname,
            parsed_url.path,
            parsed_url.query,
            parsed_url.fragment,
        ) != ("https", "example.invalid", "/${PLUGIN_ROOT}/mcp", "", ""):
            violations.append(f"remote URL was not byte-literal by components: {parsed_url!r}")
        if remote.get("headers") != {
            "X-Plugin-Data": "${PLUGIN_DATA}",
            "X-Unknown": "${UNKNOWN_VAR}",
        }:
            violations.append("remote headers were not preserved byte-for-byte")
    assert violations == [], (
        f"{_APM_REQUIREMENTS['projection']}\n"
        + "\n".join(violations)
        + f"\nreturncode={result.returncode}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def test_ambient_github_credentials_are_not_handed_to_plugin_runtime(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """ss9.2 is literal; APM-PLUGIN-SC-1 blocks unsafe target-native expansion."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "ambient", base_env=os.environ)
    environment = isolated.subprocess_env()
    environment["GITHUB_TOKEN"] = "dummy-ambient-github-token"
    environment["GITHUB_APM_PAT"] = "dummy-ambient-apm-pat"
    project = _write_project(
        isolated.work_root / "consumer",
        target="kiro",
        approve=True,
    )
    source = _copy_plugin(isolated.package_root / "ambient-plugin")
    (source / "mcp.json").write_text(
        json.dumps(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
                "mcpServers": {
                    "ambient-probe": {
                        "type": "stdio",
                        "command": "printf",
                        "env": {
                            "TOKEN_ONE": "${GITHUB_TOKEN}",
                            "TOKEN_TWO": "${GITHUB_APM_PAT}",
                        },
                    }
                },
            },
            indent=2,
        )
        + "\n",
        encoding="ascii",
    )

    result = _run(
        _runner(apm_binary_path),
        project,
        environment,
        ("install", str(source), "--target", "kiro", "--no-policy"),
        "agent-plugin-ambient-credentials",
    )
    config_text = _config_text(project, _KIRO_CONFIG)
    assert "dummy-ambient-github-token" not in config_text
    assert "dummy-ambient-apm-pat" not in config_text
    assert "${GITHUB_TOKEN}" not in config_text and "${GITHUB_APM_PAT}" not in config_text, (
        f"{_APM_REQUIREMENTS['ambient']}\nreturncode={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}\nconfig={config_text}"
    )


def test_executable_trust_denial_is_consistent_across_public_source_forms(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """APM-PLUGIN-SC-2 covers directory, archive, local-source, and Git-source forms."""
    runner = _runner(apm_binary_path)
    violations: list[str] = []

    for form in ("directory", "archive", "local-source", "git-source"):
        isolated = IsolatedApmEnvironment.create(tmp_path / form, base_env=os.environ)
        environment = isolated.subprocess_env()
        source = _copy_plugin(isolated.package_root / "plugin")
        dependencies: tuple[dict[str, object], ...] = ()
        install_args: tuple[str, ...]

        if form == "directory":
            install_args = ("install", str(source), "--target", "kiro", "--no-policy")
        elif form == "archive":
            archive = _archive_plugin(source, isolated.package_root / "plugin.zip")
            install_args = ("install", str(archive), "--target", "kiro", "--no-policy")
        elif form == "local-source":
            dependencies = ({"path": str(source)},)
            install_args = ("install", "--target", "kiro", "--no-policy")
        else:
            repositories = LocalGitRepositoryFactory(
                isolated.repository_root,
                env=environment,
            )
            repository = repositories.create("plugin", source_tree=source)
            commit = repositories.commit(repository, message="seed Agent Plugin trust source")
            remote_url = "https://gitlab.example.invalid/plugins/contract-plugin.git"
            environment = repositories.url_rewrite_subprocess_env(repository, remote_url)
            dependencies = (
                {
                    "git": remote_url,
                    "type": "gitlab",
                    "ref": commit.sha,
                    "alias": _PLUGIN_NAME,
                },
            )
            install_args = ("install", "--target", "kiro", "--no-policy")

        project = _write_project(
            isolated.work_root / "consumer",
            target="kiro",
            approve=False,
            dependencies=dependencies,
        )
        result = _run(
            runner,
            project,
            environment,
            install_args,
            f"agent-plugin-trust-{form}",
        )
        if result.returncode != 0:
            violations.append(
                f"{form}: public install returned {result.returncode}: "
                f"{result.stdout}{result.stderr}"
            )
            continue
        output = (result.stdout + result.stderr).lower()
        if not all(term in output for term in ("executable", "mcp", "lsp")):
            violations.append(f"{form}: no explicit MCP+LSP executable denial diagnostic: {output}")
        state = _runtime_state(project, _KIRO_CONFIG)
        if state.snapshot.mcp_state_bytes != (
            b'{"configs":{},"provenance":{},"servers":[],"target_servers":{}}'
        ):
            violations.append(f"{form}: denied MCP state was persisted")
        if state.snapshot.lsp_state_bytes != b'{"configs":{},"servers":[]}':
            violations.append(f"{form}: denied LSP state was persisted")
        if "contract-stdio" in _config_text(project, _KIRO_CONFIG):
            violations.append(f"{form}: denied MCP server reached target config")
        if state.snapshot.file(_SKILL_PATH.as_posix()).kind != "file":
            violations.append(f"{form}: non-executable skill was not installed")

    assert violations == [], _APM_REQUIREMENTS["trust"] + "\n" + "\n".join(violations)
