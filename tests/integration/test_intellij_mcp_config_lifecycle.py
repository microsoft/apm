"""Installed-entrypoint lifecycle coverage for JetBrains Copilot MCP config."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.artifact_snapshot import ArtifactSnapshot
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
]

_SERVER_URL = "https://example.invalid/mcp"
_UPDATED_SERVER_URL = "https://updated.example.invalid/mcp"


def _json_bytes(payload: dict) -> bytes:
    return (json.dumps(payload, indent=2) + "\n").encode("utf-8")


def _server(url: str) -> dict:
    return {
        "type": "http",
        "url": url,
        "tools": ["*"],
        "id": "",
    }


def _intellij_paths(isolated: IsolatedApmEnvironment) -> tuple[Path, Path | None, Path]:
    env = isolated.process_environment
    suffix = Path("github-copilot") / "intellij" / "mcp.json"
    data_path = Path(env["XDG_DATA_HOME"]) / suffix
    if sys.platform == "win32":
        return Path(env["LOCALAPPDATA"]) / suffix, None, data_path
    canonical = Path(env["XDG_CONFIG_HOME"]) / suffix
    if sys.platform == "darwin":
        legacy = isolated.home / "Library" / "Application Support" / suffix
    else:
        legacy = data_path
    return canonical, legacy, data_path


def _assert_changes_stay_in_scenario(
    before: ArtifactSnapshot,
    after: ArtifactSnapshot,
    scenario_name: str,
) -> None:
    difference = before.diff(after)
    changed = difference.added | difference.removed | difference.changed
    prefix = f"{scenario_name}/"
    assert all(path == scenario_name or path.startswith(prefix) for path in changed), changed


def _create_environment(tmp_path: Path, name: str) -> IsolatedApmEnvironment:
    return IsolatedApmEnvironment.create(tmp_path / name, base_env=os.environ)


def _runner(apm_binary_path: Path) -> ApmLifecycleRunner:
    return ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=300)


def _write_mcp_package(
    package: Path,
    *,
    server_name: str = "managed-server",
    server_url: str = _SERVER_URL,
) -> None:
    dump_yaml(
        {
            "name": "agent-config",
            "version": "1.0.0",
            "dependencies": {
                "mcp": [
                    {
                        "name": server_name,
                        "registry": False,
                        "transport": "http",
                        "url": server_url,
                    }
                ]
            },
        },
        package / "apm.yml",
    )


def _write_consumer(project: Path, package_ref: str, name: str) -> None:
    dump_yaml(
        {
            "name": name,
            "version": "1.0.0",
            "dependencies": {"apm": [package_ref]},
        },
        project / "apm.yml",
    )


def test_direct_install_uses_config_state_for_full_lifecycle(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Direct install, repeat, update, reinstall, and audit stay canonical."""
    isolated = _create_environment(tmp_path, "direct")
    project = isolated.work_root / "consumer"
    project.mkdir()
    dump_yaml(
        {
            "name": "intellij-direct-consumer",
            "version": "1.0.0",
        },
        project / "apm.yml",
    )
    environment = isolated.subprocess_env(overrides={"APM_NON_INTERACTIVE": "1"})
    canonical, legacy, data_path = _intellij_paths(isolated)
    outside_before = ArtifactSnapshot.capture(tmp_path)
    install = (
        "install",
        "--mcp",
        "direct-server",
        "--transport",
        "http",
        "--url",
        _SERVER_URL,
        "--target",
        "intellij",
        "--no-policy",
    )
    update = (*install, "--force")
    update = tuple(_UPDATED_SERVER_URL if value == _SERVER_URL else value for value in update)

    results = _runner(apm_binary_path).run_sequence(
        (
            install,
            install,
            update,
            ("install", "--target", "intellij", "--no-policy"),
        ),
        expected_returncodes=(0, 0, 0, 0),
        scenario_id="intellij-direct-lifecycle",
        cwd=project,
        env=environment,
    )

    expected = _json_bytes({"servers": {"direct-server": _server(_UPDATED_SERVER_URL)}})
    assert canonical.read_bytes() == expected
    assert not data_path.exists()
    if legacy is not None:
        assert not legacy.exists()
    assert all("xdg-data" not in result.stdout for result in results)

    if legacy is not None:
        canonical.unlink()
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_bytes(
            _json_bytes(
                {
                    "legacySetting": "keep",
                    "servers": {
                        "direct-server": _server(_UPDATED_SERVER_URL),
                        "legacy-user": {"command": "user-owned"},
                    },
                }
            )
        )
        updated_install = tuple(
            _UPDATED_SERVER_URL if value == _SERVER_URL else value for value in install
        )
        migration_results = _runner(apm_binary_path).run_sequence(
            (updated_install,),
            expected_returncodes=(0,),
            scenario_id="intellij-direct-skipped-migration",
            cwd=project,
            env=environment,
        )
        assert "[+] Migrated 1 IntelliJ MCP server to" in (
            migration_results[0].stdout + migration_results[0].stderr
        )
        assert canonical.read_bytes() == expected
        assert legacy.read_bytes() == _json_bytes(
            {
                "legacySetting": "keep",
                "servers": {"legacy-user": {"command": "user-owned"}},
            }
        )

    _runner(apm_binary_path).run_sequence(
        (("audit", "--ci", "--no-policy", "--no-drift"),),
        expected_returncodes=(0,),
        scenario_id="intellij-direct-audit",
        cwd=project,
        env=environment,
    )
    outside_after = ArtifactSnapshot.capture(tmp_path)
    _assert_changes_stay_in_scenario(outside_before, outside_after, isolated.root.name)


def test_transitive_package_migrates_owned_entries_and_cleans_by_provenance(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Package install migrates, contracts, audits, and uninstalls safely."""
    isolated = _create_environment(tmp_path, "package")
    package = isolated.package_root / "agent-config"
    package.mkdir()
    dump_yaml(
        {
            "name": "agent-config",
            "version": "1.0.0",
            "dependencies": {
                "mcp": [
                    {
                        "name": "managed-server",
                        "registry": False,
                        "transport": "http",
                        "url": _SERVER_URL,
                    }
                ]
            },
        },
        package / "apm.yml",
    )
    project = isolated.work_root / "consumer"
    project.mkdir()
    package_ref = "../../packages/agent-config"
    dump_yaml(
        {
            "name": "intellij-package-consumer",
            "version": "1.0.0",
            "dependencies": {"apm": [package_ref]},
        },
        project / "apm.yml",
    )
    environment = isolated.subprocess_env(overrides={"APM_NON_INTERACTIVE": "1"})
    canonical, legacy, data_path = _intellij_paths(isolated)
    canonical.parent.mkdir(parents=True)
    canonical_user = {"command": "user-owned"}
    canonical.write_bytes(
        _json_bytes(
            {
                "setting": "keep",
                "servers": {"canonical-user": canonical_user},
            }
        )
    )
    outside_before = ArtifactSnapshot.capture(tmp_path)
    install = (
        "install",
        "--target",
        "intellij",
        "--trust-transitive-mcp",
        "--no-policy",
    )

    _runner(apm_binary_path).run_sequence(
        (install, install),
        expected_returncodes=(0, 0),
        scenario_id="intellij-package-install-repeat",
        cwd=project,
        env=environment,
    )

    expected_config = {
        "setting": "keep",
        "servers": {
            "canonical-user": canonical_user,
            "managed-server": _server(_SERVER_URL),
        },
    }
    assert canonical.read_bytes() == _json_bytes(expected_config)
    assert not data_path.exists()
    if legacy is not None:
        assert not legacy.exists()

    package_data = {
        "name": "agent-config",
        "version": "1.0.0",
        "dependencies": {
            "mcp": [
                {
                    "name": "managed-server",
                    "registry": False,
                    "transport": "http",
                    "url": _UPDATED_SERVER_URL,
                }
            ]
        },
    }
    dump_yaml(package_data, package / "apm.yml")
    update_install = (*install, "--update")
    _runner(apm_binary_path).run_sequence(
        (
            update_install,
            ("audit", "--ci", "--no-policy", "--no-drift"),
        ),
        expected_returncodes=(0, 0),
        scenario_id="intellij-package-update-audit",
        cwd=project,
        env=environment,
    )
    expected_config["servers"]["managed-server"] = _server(_UPDATED_SERVER_URL)
    assert canonical.read_bytes() == _json_bytes(expected_config)

    if legacy is not None:
        canonical.write_bytes(
            _json_bytes(
                {
                    "setting": "keep",
                    "servers": {"canonical-user": canonical_user},
                }
            )
        )
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_bytes(
            _json_bytes(
                {
                    "legacySetting": "keep",
                    "servers": {
                        "managed-server": _server(_UPDATED_SERVER_URL),
                        "legacy-user": {"command": "user-owned"},
                    },
                }
            )
        )

        _runner(apm_binary_path).run_sequence(
            (install,),
            expected_returncodes=(0,),
            scenario_id="intellij-package-migration",
            cwd=project,
            env=environment,
        )

        assert canonical.read_bytes() == _json_bytes(expected_config)
        assert legacy.read_bytes() == _json_bytes(
            {
                "legacySetting": "keep",
                "servers": {"legacy-user": {"command": "user-owned"}},
            }
        )

    _runner(apm_binary_path).run_sequence(
        (
            (
                "install",
                "--target",
                "vscode",
                "--trust-transitive-mcp",
                "--no-policy",
            ),
            install,
            ("uninstall", package_ref),
        ),
        expected_returncodes=(0, 0, 0),
        scenario_id="intellij-package-contraction-uninstall",
        cwd=project,
        env=environment,
    )

    assert canonical.read_bytes() == _json_bytes(
        {
            "setting": "keep",
            "servers": {"canonical-user": canonical_user},
        }
    )
    if legacy is not None:
        assert legacy.read_bytes() == _json_bytes(
            {
                "legacySetting": "keep",
                "servers": {"legacy-user": {"command": "user-owned"}},
            }
        )
    outside_after = ArtifactSnapshot.capture(tmp_path)
    _assert_changes_stay_in_scenario(outside_before, outside_after, isolated.root.name)


def test_malformed_installed_destination_fails_without_rewrite(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """The installed entrypoint fails closed on malformed user config."""
    isolated = _create_environment(tmp_path, "malformed")
    project = isolated.work_root / "consumer"
    project.mkdir()
    dump_yaml(
        {
            "name": "intellij-malformed-consumer",
            "version": "1.0.0",
        },
        project / "apm.yml",
    )
    canonical, legacy, data_path = _intellij_paths(isolated)
    canonical.parent.mkdir(parents=True)
    original = b"{malformed\n"
    canonical.write_bytes(original)
    environment = isolated.subprocess_env(overrides={"APM_NON_INTERACTIVE": "1"})

    result = _runner(apm_binary_path).run(
        (
            "install",
            "--mcp",
            "malformed-server",
            "--transport",
            "http",
            "--url",
            _SERVER_URL,
            "--target",
            "intellij",
            "--no-policy",
        ),
        scenario_id="intellij-malformed-destination",
        cwd=project,
        env=environment,
    )

    assert result.returncode == 1
    assert "MCP configuration failed for selected runtime(s)" in result.stderr + result.stdout
    assert "is malformed JSON" in result.stderr + result.stdout
    assert "Fix the file, then rerun apm install" in result.stderr + result.stdout
    assert "MCP server written to apm.yml" not in result.stderr + result.stdout
    assert "Run with --verbose" not in result.stderr + result.stdout
    assert canonical.read_bytes() == original
    assert not data_path.exists()
    if legacy is not None:
        assert not legacy.exists()


def test_uninstall_with_malformed_intellij_config_is_nonzero(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Uninstall fails explicitly without rewriting malformed IntelliJ JSON."""
    isolated = _create_environment(tmp_path, "malformed-uninstall")
    package = isolated.package_root / "agent-config"
    package.mkdir()
    dump_yaml(
        {
            "name": "agent-config",
            "version": "1.0.0",
            "dependencies": {
                "mcp": [
                    {
                        "name": "managed-server",
                        "registry": False,
                        "transport": "http",
                        "url": _SERVER_URL,
                    }
                ]
            },
        },
        package / "apm.yml",
    )
    project = isolated.work_root / "consumer"
    project.mkdir()
    package_ref = "../../packages/agent-config"
    dump_yaml(
        {
            "name": "intellij-malformed-uninstall",
            "version": "1.0.0",
            "dependencies": {"apm": [package_ref]},
        },
        project / "apm.yml",
    )
    environment = isolated.subprocess_env(overrides={"APM_NON_INTERACTIVE": "1"})
    canonical, _legacy, _data_path = _intellij_paths(isolated)
    install = _runner(apm_binary_path).run(
        (
            "install",
            "--target",
            "intellij",
            "--trust-transitive-mcp",
            "--no-policy",
        ),
        scenario_id="intellij-malformed-uninstall-setup",
        cwd=project,
        env=environment,
    )
    assert install.returncode == 0, install.stderr + install.stdout
    original = b"{malformed\n"
    canonical.write_bytes(original)

    uninstall = _runner(apm_binary_path).run(
        ("uninstall", package_ref),
        scenario_id="intellij-malformed-uninstall",
        cwd=project,
        env=environment,
    )

    assert uninstall.returncode == 1
    assert "malformed JSON" in uninstall.stderr + uninstall.stdout
    assert "Uninstall incomplete" in uninstall.stderr + uninstall.stdout
    assert "Uninstall complete" not in uninstall.stderr + uninstall.stdout
    normalized_output = " ".join((uninstall.stderr + uninstall.stdout).split())
    assert normalized_output.count("rerun apm install") == 1
    assert canonical.read_bytes() == original


def test_uninstall_does_not_touch_unowned_intellij_jsonc(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Claude-scoped uninstall leaves same-name IntelliJ JSONC untouched."""
    isolated = _create_environment(tmp_path, "scoped-uninstall")
    package = isolated.package_root / "agent-config"
    package.mkdir()
    dump_yaml(
        {
            "name": "agent-config",
            "version": "1.0.0",
            "dependencies": {
                "mcp": [
                    {
                        "name": "managed-server",
                        "registry": False,
                        "transport": "http",
                        "url": _SERVER_URL,
                    }
                ]
            },
        },
        package / "apm.yml",
    )
    project = isolated.work_root / "consumer"
    project.mkdir()
    package_ref = "../../packages/agent-config"
    dump_yaml(
        {
            "name": "scoped-uninstall-consumer",
            "version": "1.0.0",
            "dependencies": {"apm": [package_ref]},
        },
        project / "apm.yml",
    )
    canonical, _legacy, _data_path = _intellij_paths(isolated)
    canonical.parent.mkdir(parents=True)
    original = (
        b"{\n"
        b"  // user-owned entry\n"
        b'  "servers": {\n'
        b'    "managed-server": {"type": "http", "url": "https://example.invalid/mcp"}\n'
        b"  }\n"
        b"}\n"
    )
    canonical.write_bytes(original)
    environment = isolated.subprocess_env(overrides={"APM_NON_INTERACTIVE": "1"})
    runner = _runner(apm_binary_path)

    install = runner.run(
        (
            "install",
            "--target",
            "claude",
            "--trust-transitive-mcp",
            "--no-policy",
        ),
        scenario_id="scoped-uninstall-setup",
        cwd=project,
        env=environment,
    )
    assert install.returncode == 0, install.stderr + install.stdout

    uninstall = runner.run(
        ("uninstall", package_ref),
        scenario_id="scoped-uninstall",
        cwd=project,
        env=environment,
    )

    assert uninstall.returncode == 0, uninstall.stderr + uninstall.stdout
    assert canonical.read_bytes() == original
    claude_config = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))
    assert "managed-server" not in claude_config["mcpServers"]


def test_uninstall_cleans_owned_intellij_jsonc_without_corrupting_url(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Owned JSONC cleanup preserves an unrelated URL containing slashes."""
    isolated = _create_environment(tmp_path, "owned-jsonc-uninstall")
    package = isolated.package_root / "agent-config"
    package.mkdir()
    _write_mcp_package(package)
    project = isolated.work_root / "consumer"
    project.mkdir()
    package_ref = "../../packages/agent-config"
    _write_consumer(project, package_ref, "owned-jsonc-uninstall-consumer")
    environment = isolated.subprocess_env(overrides={"APM_NON_INTERACTIVE": "1"})
    canonical, _legacy, _data_path = _intellij_paths(isolated)
    runner = _runner(apm_binary_path)
    install = runner.run(
        (
            "install",
            "--target",
            "intellij",
            "--trust-transitive-mcp",
            "--no-policy",
        ),
        scenario_id="owned-jsonc-uninstall-setup",
        cwd=project,
        env=environment,
    )
    assert install.returncode == 0, install.stderr + install.stdout
    retained_url = "https://user.example.invalid/mcp"
    canonical.write_text(
        (
            "{\n"
            "  // preserve this user entry\n"
            '  "servers": {\n'
            f'    "managed-server": {json.dumps(_server(_SERVER_URL))},\n'
            f'    "user-server": {json.dumps(_server(retained_url))},\n'
            "  },\n"
            "}\n"
        ),
        encoding="utf-8",
    )

    uninstall = runner.run(
        ("uninstall", package_ref),
        scenario_id="owned-jsonc-uninstall",
        cwd=project,
        env=environment,
    )

    assert uninstall.returncode == 0, uninstall.stderr + uninstall.stdout
    config = json.loads(canonical.read_text(encoding="utf-8"))
    assert set(config["servers"]) == {"user-server"}
    assert config["servers"]["user-server"]["url"] == retained_url


def test_uninstall_continues_after_intellij_cleanup_failure(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A failed IntelliJ target does not prevent later VS Code cleanup."""
    isolated = _create_environment(tmp_path, "continued-uninstall")
    package = isolated.package_root / "agent-config"
    package.mkdir()
    _write_mcp_package(package)
    project = isolated.work_root / "consumer"
    project.mkdir()
    package_ref = "../../packages/agent-config"
    _write_consumer(project, package_ref, "continued-uninstall-consumer")
    environment = isolated.subprocess_env(overrides={"APM_NON_INTERACTIVE": "1"})
    canonical, _legacy, _data_path = _intellij_paths(isolated)
    runner = _runner(apm_binary_path)
    install = runner.run(
        (
            "install",
            "--target",
            "intellij,vscode",
            "--trust-transitive-mcp",
            "--no-policy",
        ),
        scenario_id="continued-uninstall-setup",
        cwd=project,
        env=environment,
    )
    assert install.returncode == 0, install.stderr + install.stdout
    vscode_path = project / ".vscode" / "mcp.json"
    assert "managed-server" in json.loads(vscode_path.read_text(encoding="utf-8"))["servers"]
    canonical.write_bytes(b"{malformed\n")

    uninstall = runner.run(
        ("uninstall", package_ref),
        scenario_id="continued-uninstall",
        cwd=project,
        env=environment,
    )

    assert uninstall.returncode == 1
    assert "managed-server" not in json.loads(vscode_path.read_text(encoding="utf-8"))["servers"]
    output = uninstall.stderr + uninstall.stdout
    assert "Uninstall incomplete" in output
    assert "run 'apm install'" in " ".join(output.split())


def test_uninstall_preserves_legacy_ownership_for_surviving_server(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Sequential legacy uninstalls retain ownership until each server is removed."""
    isolated = _create_environment(tmp_path, "legacy-sequential-uninstall")
    first_package = isolated.package_root / "first-config"
    second_package = isolated.package_root / "second-config"
    first_package.mkdir()
    second_package.mkdir()
    _write_mcp_package(first_package, server_name="first-server")
    _write_mcp_package(
        second_package,
        server_name="second-server",
        server_url=_UPDATED_SERVER_URL,
    )
    project = isolated.work_root / "consumer"
    project.mkdir()
    first_ref = "../../packages/first-config"
    second_ref = "../../packages/second-config"
    dump_yaml(
        {
            "name": "legacy-sequential-uninstall-consumer",
            "version": "1.0.0",
            "dependencies": {"apm": [first_ref, second_ref]},
        },
        project / "apm.yml",
    )
    environment = isolated.subprocess_env(overrides={"APM_NON_INTERACTIVE": "1"})
    runner = _runner(apm_binary_path)
    install = runner.run(
        (
            "install",
            "--target",
            "intellij",
            "--trust-transitive-mcp",
            "--no-policy",
        ),
        scenario_id="legacy-sequential-uninstall-setup",
        cwd=project,
        env=environment,
    )
    assert install.returncode == 0, install.stderr + install.stdout
    lock_path = project / "apm.lock.yaml"
    legacy_lock = load_yaml(lock_path)
    legacy_lock.pop("mcp_target_servers")
    dump_yaml(legacy_lock, lock_path)

    first_uninstall = runner.run(
        ("uninstall", first_ref),
        scenario_id="legacy-sequential-uninstall-first",
        cwd=project,
        env=environment,
    )

    assert first_uninstall.returncode == 0, first_uninstall.stderr + first_uninstall.stdout
    canonical, _legacy, _data_path = _intellij_paths(isolated)
    first_config = json.loads(canonical.read_text(encoding="utf-8"))["servers"]
    assert set(first_config) == {"second-server"}
    contracted_lock = load_yaml(lock_path)
    assert contracted_lock["mcp_target_servers"] == {"intellij": ["second-server"]}

    second_uninstall = runner.run(
        ("uninstall", second_ref),
        scenario_id="legacy-sequential-uninstall-second",
        cwd=project,
        env=environment,
    )

    assert second_uninstall.returncode == 0, second_uninstall.stderr + second_uninstall.stdout
    second_config = json.loads(canonical.read_text(encoding="utf-8"))["servers"]
    assert second_config == {}


def test_uninstall_treats_explicit_empty_ownership_as_noop(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """An explicit empty ownership map never authorizes target cleanup."""
    isolated = _create_environment(tmp_path, "explicit-empty-uninstall")
    package = isolated.package_root / "agent-config"
    package.mkdir()
    _write_mcp_package(package)
    project = isolated.work_root / "consumer"
    project.mkdir()
    package_ref = "../../packages/agent-config"
    _write_consumer(project, package_ref, "explicit-empty-uninstall-consumer")
    environment = isolated.subprocess_env(overrides={"APM_NON_INTERACTIVE": "1"})
    runner = _runner(apm_binary_path)
    install = runner.run(
        (
            "install",
            "--target",
            "intellij",
            "--trust-transitive-mcp",
            "--no-policy",
        ),
        scenario_id="explicit-empty-uninstall-setup",
        cwd=project,
        env=environment,
    )
    assert install.returncode == 0, install.stderr + install.stdout
    lock_path = project / "apm.lock.yaml"
    explicit_empty_lock = load_yaml(lock_path)
    explicit_empty_lock["mcp_target_servers"] = {}
    dump_yaml(explicit_empty_lock, lock_path)
    canonical, _legacy, _data_path = _intellij_paths(isolated)
    original = canonical.read_bytes()

    uninstall = runner.run(
        ("uninstall", package_ref),
        scenario_id="explicit-empty-uninstall",
        cwd=project,
        env=environment,
    )

    assert uninstall.returncode == 0, uninstall.stderr + uninstall.stdout
    assert canonical.read_bytes() == original


def test_install_without_mcp_respects_explicit_empty_ownership(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A zero-MCP install never cleans targets when ownership is explicitly empty."""
    isolated = _create_environment(tmp_path, "explicit-empty-reinstall")
    package = isolated.package_root / "agent-config"
    package.mkdir()
    _write_mcp_package(package)
    project = isolated.work_root / "consumer"
    project.mkdir()
    package_ref = "../../packages/agent-config"
    _write_consumer(project, package_ref, "explicit-empty-reinstall-consumer")
    environment = isolated.subprocess_env(overrides={"APM_NON_INTERACTIVE": "1"})
    runner = _runner(apm_binary_path)
    install_args = (
        "install",
        "--target",
        "intellij",
        "--trust-transitive-mcp",
        "--no-policy",
    )
    initial_install = runner.run(
        install_args,
        scenario_id="explicit-empty-reinstall-setup",
        cwd=project,
        env=environment,
    )
    assert initial_install.returncode == 0, initial_install.stderr + initial_install.stdout
    lock_path = project / "apm.lock.yaml"
    explicit_empty_lock = load_yaml(lock_path)
    explicit_empty_lock["mcp_target_servers"] = {}
    dump_yaml(explicit_empty_lock, lock_path)
    dump_yaml(
        {"name": "explicit-empty-reinstall-consumer", "version": "1.0.0"},
        project / "apm.yml",
    )
    canonical, _legacy, _data_path = _intellij_paths(isolated)
    original = canonical.read_bytes()

    reinstall = runner.run(
        install_args,
        scenario_id="explicit-empty-reinstall",
        cwd=project,
        env=environment,
    )

    assert reinstall.returncode == 0, reinstall.stderr + reinstall.stdout
    assert canonical.read_bytes() == original
