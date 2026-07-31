"""Installed-entrypoint lifecycle coverage for JetBrains Copilot MCP config."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
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
