from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.utils.isolated_apm_environment import IsolatedApmEnvironment


def test_create_builds_unique_scenario_roots(tmp_path: Path) -> None:
    first = IsolatedApmEnvironment.create(
        tmp_path / "first",
        base_env=os.environ,
    )
    second = IsolatedApmEnvironment.create(
        tmp_path / "second",
        base_env=os.environ,
    )

    for attribute in (
        "root",
        "home",
        "config_root",
        "cache_root",
        "package_root",
        "repository_root",
        "work_root",
        "temp_root",
    ):
        assert getattr(first, attribute) != getattr(second, attribute)
        assert getattr(first, attribute).is_dir()
        assert getattr(second, attribute).is_dir()

    for isolated in (first, second):
        environment = isolated.subprocess_env()
        expected_bindings = {
            "HOME": isolated.home,
            "APM_HOME": isolated.config_root,
            "APM_CACHE_DIR": isolated.cache_root,
            "APM_TEMP_DIR": isolated.temp_root,
        }
        for name, expected_root in expected_bindings.items():
            assert Path(environment[name]) == expected_root
            assert expected_root.resolve().is_relative_to(isolated.root.resolve())


def test_create_rejects_reused_root(tmp_path: Path) -> None:
    root = tmp_path / "scenario"
    IsolatedApmEnvironment.create(root, base_env=os.environ)

    with pytest.raises(FileExistsError):
        IsolatedApmEnvironment.create(root, base_env=os.environ)


def test_environment_does_not_mutate_parent_process(tmp_path: Path) -> None:
    parent_environment = os.environ.copy()
    parent_cwd = Path.cwd()

    canonical_first = IsolatedApmEnvironment.create(
        tmp_path / "canonical-first",
        base_env={"PATH": "canonical-path", "Path": "case-variant-path"},
    )
    canonical_last = IsolatedApmEnvironment.create(
        tmp_path / "canonical-last",
        base_env={"Path": "case-variant-path", "PATH": "canonical-path"},
    )
    for isolated in (canonical_first, canonical_last):
        environment = isolated.subprocess_env()
        assert environment["PATH"] == "canonical-path"
        assert "Path" not in environment

    child_environment = canonical_first.subprocess_env(
        overrides={"Path": "scenario-path", "SCENARIO": "one"}
    )

    assert child_environment["SCENARIO"] == "one"
    assert child_environment["Path"] == "scenario-path"
    assert "PATH" not in child_environment
    normalized_names = [name.upper() for name in child_environment]
    assert len(normalized_names) == len(set(normalized_names))
    assert os.environ == parent_environment
    assert Path.cwd() == parent_cwd


def test_protected_environment_overrides_are_rejected(tmp_path: Path) -> None:
    ambient_environment = dict(os.environ)
    ambient_environment.update(
        {
            "APM_NO_CACHE": "1",
            "APM_REGISTRY_PASS_INTERNAL": "ambient",
            "APM_REGISTRY_TOKEN_INTERNAL": "ambient",
            "APM_REGISTRY_USER_INTERNAL": "ambient",
            "CLAUDE_CONFIG_DIR": "/ambient/claude",
            "CODEX_HOME": "/ambient/codex",
            "COPILOT_HOME": "/ambient/copilot",
            "GITHUB_APM_PAT_ACME": "ambient",
            "GITHUB_TOKEN": "ambient",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "ambient",
            "GIT_DIR": "/ambient/git",
            "HERMES_HOME": "/ambient/hermes",
            "HTTPS_PROXY": "https://ambient.invalid",
            "Git_Config_Value_1": "ambient",
            "github_token": "ambient",
            "git_config_key_1": "credential.helper",
            "home": "/ambient/home",
        }
    )
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "scenario",
        base_env=ambient_environment,
    )
    environment = isolated.subprocess_env()

    stripped_names = (
        "APM_NO_CACHE",
        "APM_REGISTRY_PASS_INTERNAL",
        "APM_REGISTRY_TOKEN_INTERNAL",
        "APM_REGISTRY_USER_INTERNAL",
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "COPILOT_HOME",
        "GITHUB_APM_PAT_ACME",
        "GITHUB_TOKEN",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "GIT_DIR",
        "HERMES_HOME",
        "HTTPS_PROXY",
        "Git_Config_Value_1",
        "github_token",
        "git_config_key_1",
    )
    normalized_environment_names = {name.upper() for name in environment}
    assert {name.upper() for name in stripped_names}.isdisjoint(normalized_environment_names)
    assert len(environment) == len(normalized_environment_names)
    assert "home" not in environment
    assert environment["HOME"] == str(isolated.home)
    for name in ("GH_CONFIG_DIR", "AZURE_CONFIG_DIR", "GIT_CONFIG_GLOBAL"):
        assert Path(environment[name]).resolve().is_relative_to(isolated.root.resolve())

    exact_names = ("HOME", "GIT_ALLOW_PROTOCOL", "GITHUB_TOKEN", "PYTHONPATH")
    tool_home_names = ("CLAUDE_CONFIG_DIR", "APM_TEMP_DIR")
    dynamic_names = (
        "GITHUB_APM_PAT_ACME",
        "APM_REGISTRY_TOKEN_INTERNAL",
        "GIT_CONFIG_KEY_0",
        "github_apm_pat_acme",
        "apm_registry_token_internal",
        "git_config_key_0",
    )
    case_variant_exact_names = ("home", "github_token", "Git_Allow_Protocol")
    for name in exact_names + case_variant_exact_names + tool_home_names + dynamic_names:
        with pytest.raises(ValueError, match="protected environment"):
            isolated.subprocess_env(overrides={name: "unsafe"})


def test_python_child_network_is_denied(tmp_path: Path) -> None:
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "scenario",
        base_env=os.environ,
    )

    scripts = (
        "import socket; socket.create_connection(('example.invalid', 443))",
        (
            "import socket; "
            "socket.socket(socket.AF_INET, socket.SOCK_STREAM)"
            ".connect(('203.0.113.1', 443))"
        ),
        (
            "import socket; "
            "socket.socket(socket.AF_INET6, socket.SOCK_STREAM)"
            ".connect(('2001:db8::1', 443))"
        ),
        (
            "import socket; "
            "socket.socket(socket.AF_INET, socket.SOCK_DGRAM)"
            ".sendto(b'x', ('203.0.113.1', 9))"
        ),
        (
            "import socket; "
            "socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)"
            ".sendto(b'x', ('2001:db8::1', 9))"
        ),
    )
    for script in scripts:
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=isolated.work_root,
            env=isolated.subprocess_env(),
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode != 0
        assert result.stderr.count("IP network disabled by test environment") == 1


def test_git_rejects_non_file_transport(tmp_path: Path) -> None:
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "scenario",
        base_env=os.environ,
    )

    result = subprocess.run(
        ["git", "ls-remote", "https://example.invalid/repository", "HEAD"],
        cwd=isolated.work_root,
        env=isolated.subprocess_env(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stderr.count("transport 'https' not allowed") == 1
