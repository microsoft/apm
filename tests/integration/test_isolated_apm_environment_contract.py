from __future__ import annotations

import inspect
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from pathlib import Path

import pytest

from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ROOT_ATTRIBUTES = (
    "root",
    "home",
    "config_root",
    "cache_root",
    "package_root",
    "repository_root",
    "work_root",
    "temp_root",
)


def test_create_builds_unique_scenario_roots(tmp_path: Path) -> None:
    expected_fields = (
        "root",
        "home",
        "config_root",
        "cache_root",
        "package_root",
        "repository_root",
        "work_root",
        "temp_root",
        "process_environment",
    )
    assert tuple(field.name for field in fields(IsolatedApmEnvironment)) == expected_fields
    expected_field_annotations = {
        "root": "Path",
        "home": "Path",
        "config_root": "Path",
        "cache_root": "Path",
        "package_root": "Path",
        "repository_root": "Path",
        "work_root": "Path",
        "temp_root": "Path",
        "process_environment": "Mapping[str, str]",
    }
    assert IsolatedApmEnvironment.__annotations__ == expected_field_annotations
    assert {field.name: field.type for field in fields(IsolatedApmEnvironment)} == (
        expected_field_annotations
    )
    public_methods = {
        name
        for name, member in inspect.getmembers(
            IsolatedApmEnvironment,
            predicate=inspect.isroutine,
        )
        if not name.startswith("_")
    }
    public_callables = {
        name
        for name in dir(IsolatedApmEnvironment)
        if not name.startswith("_") and callable(getattr(IsolatedApmEnvironment, name))
    }
    assert public_methods == {"create", "subprocess_env"}
    assert public_callables == {"create", "subprocess_env"}
    create_signature = inspect.signature(IsolatedApmEnvironment.create)
    create_parameters = create_signature.parameters
    assert tuple(create_parameters) == ("root", "base_env")
    assert create_parameters["root"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert create_parameters["root"].default is inspect.Parameter.empty
    assert create_parameters["root"].annotation == "Path"
    assert create_parameters["base_env"].kind is inspect.Parameter.KEYWORD_ONLY
    assert create_parameters["base_env"].default is inspect.Parameter.empty
    assert create_parameters["base_env"].annotation == "Mapping[str, str]"
    assert create_signature.return_annotation == "IsolatedApmEnvironment"
    subprocess_signature = inspect.signature(IsolatedApmEnvironment.subprocess_env)
    subprocess_parameters = subprocess_signature.parameters
    assert tuple(subprocess_parameters) == ("self", "overrides")
    assert subprocess_parameters["self"].annotation is inspect.Parameter.empty
    assert subprocess_parameters["overrides"].kind is inspect.Parameter.KEYWORD_ONLY
    assert subprocess_parameters["overrides"].default is None
    assert subprocess_parameters["overrides"].annotation == "Mapping[str, str] | None"
    assert subprocess_signature.return_annotation == "dict[str, str]"

    first = IsolatedApmEnvironment.create(
        tmp_path / "first",
        base_env=os.environ,
    )
    second = IsolatedApmEnvironment.create(
        tmp_path / "second",
        base_env=os.environ,
    )
    with pytest.raises(AttributeError):
        first.root = tmp_path / "mutated"
    with pytest.raises(TypeError):
        first.process_environment["GITHUB_TOKEN"] = "injected"
    with pytest.raises(TypeError):
        first.process_environment["GIT_DIR"] = "/injected/git"
    immutable_child_environment = first.subprocess_env()
    assert "GITHUB_TOKEN" not in immutable_child_environment
    assert "GIT_DIR" not in immutable_child_environment

    for attribute in _ROOT_ATTRIBUTES:
        assert getattr(first, attribute) != getattr(second, attribute)
        assert getattr(first, attribute).is_dir()
        assert getattr(second, attribute).is_dir()

    for isolated in (first, second):
        for attribute in _ROOT_ATTRIBUTES:
            generated_root = getattr(isolated, attribute).resolve()
            assert generated_root.is_relative_to(isolated.root.resolve())

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

    scenario_roots = tuple(tmp_path / f"parallel-{index}" / "scenario" for index in range(8))
    with ThreadPoolExecutor(max_workers=len(scenario_roots)) as executor:
        concurrent_environments = tuple(
            executor.map(
                lambda root: IsolatedApmEnvironment.create(
                    root,
                    base_env=os.environ,
                ),
                scenario_roots,
            )
        )

    assert {isolated.root.name for isolated in concurrent_environments} == {"scenario"}
    generated_roots = {
        getattr(isolated, attribute).resolve()
        for isolated in concurrent_environments
        for attribute in _ROOT_ATTRIBUTES
    }
    assert len(generated_roots) == len(concurrent_environments) * len(_ROOT_ATTRIBUTES)
    for isolated in concurrent_environments:
        for attribute in _ROOT_ATTRIBUTES:
            generated_root = getattr(isolated, attribute).resolve()
            assert generated_root.is_relative_to(isolated.root.resolve())

    with pytest.raises(TypeError):
        IsolatedApmEnvironment.create(tmp_path / "positional", {})
    with pytest.raises(TypeError):
        first.subprocess_env({})


def test_create_rejects_reused_root(tmp_path: Path) -> None:
    root = tmp_path / "scenario"
    IsolatedApmEnvironment.create(root, base_env=os.environ)

    with pytest.raises(FileExistsError):
        IsolatedApmEnvironment.create(root, base_env=os.environ)

    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    with pytest.raises(FileExistsError):
        IsolatedApmEnvironment.create(empty_root, base_env=os.environ)


def test_environment_does_not_mutate_parent_process(tmp_path: Path) -> None:
    fresh_process_root = tmp_path / "fresh-process"
    script = """\
import os
import sys
from pathlib import Path

mutation = os.environ.pop("CENV_MUTATION", "")
parent_environment = os.environ.copy()
parent_cwd = Path.cwd()
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

if mutation == "mutate-parent":
    os.environ["CENV_MUTATED"] = "1"
IsolatedApmEnvironment.create(Path(sys.argv[1]), base_env=os.environ)
if os.environ != parent_environment:
    raise AssertionError("create mutated parent environment")
if Path.cwd() != parent_cwd:
    raise AssertionError("create mutated parent cwd")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(fresh_process_root)],
        cwd=_PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    mutated_environment = dict(os.environ)
    mutated_environment["CENV_MUTATION"] = "mutate-parent"
    mutation_result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "mutated-process")],
        cwd=_PROJECT_ROOT,
        env=mutated_environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert mutation_result.returncode != 0
    assert mutation_result.stderr.count("create mutated parent environment") == 1

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
    security_control_names = (
        "APM_ALLOW_PROTOCOL_FALLBACK",
        "APM_COPILOT_APP_DB",
        "APM_COPILOT_APP_WS_RUN_DIR",
        "APM_COPILOT_COWORK_SKILLS_DIR",
        "APM_DISABLE_TRUSTSTORE",
        "APM_E2E_TESTS",
        "APM_GITLAB_HOSTS",
        "APM_GIT_PROTOCOL",
        "APM_INSTALLER_BASE_URL",
        "APM_NO_DIRECT_FALLBACK",
        "APM_POLICY_DISABLE",
        "APM_PYPI_INDEX_URL",
        "APM_RELEASE_BASE_URL",
        "APM_RELEASE_METADATA_URL",
        "APM_REPO",
        "APM_SSL_CERT_FILE_IS_BUNDLED_DEFAULT",
        "ARTIFACTORY_BASE_URL",
        "ARTIFACTORY_MAX_ARCHIVE_MB",
        "ARTIFACTORY_ONLY",
        "CURL_CA_BUNDLE",
        "GITHUB_HOST",
        "GITHUB_URL",
        "GITLAB_HOST",
        "MCP_REGISTRY_ALLOW_HTTP",
        "MCP_REGISTRY_URL",
        "PROXY_REGISTRY_ALLOW_HTTP",
        "PROXY_REGISTRY_ONLY",
        "PROXY_REGISTRY_URL",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_FILE",
    )
    exact_secret_names = (
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
        "ACTIONS_ID_TOKEN_REQUEST_URL",
        "ACTIONS_RUNTIME_TOKEN",
        "ADO_APM_PAT",
        "ARTIFACTORY_APM_TOKEN",
        "AZURE_DEVOPS_EXT_PAT",
        "COPILOT_GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GH_TOKEN",
        "GITHUB_APM_PAT",
        "GITHUB_COPILOT_PAT",
        "GITHUB_ENTERPRISE_TOKEN",
        "GITHUB_MODELS_KEY",
        "GITHUB_PERSONAL_ACCESS_TOKEN",
        "GITHUB_TOKEN",
        "GITLAB_APM_PAT",
        "GITLAB_TOKEN",
        "GIT_ASKPASS",
        "GIT_TOKEN",
        "NVIDIA_INFERENCE_KEY",
        "OPENAI_API_KEY",
        "PROXY_REGISTRY_TOKEN",
        "SSH_ASKPASS",
    )
    credential_pattern_names = (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "SERVICE_API_KEY",
        "SERVICE_PASSWORD",
        "SERVICE_PAT",
        "SERVICE_SECRET",
        "SERVICE_TOKEN",
    )
    pinned_names = (
        "HOME",
        "USERPROFILE",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "LOCALAPPDATA",
        "APM_HOME",
        "APM_CACHE_DIR",
        "APM_TEMP_DIR",
        "GH_CONFIG_DIR",
        "AZURE_CONFIG_DIR",
        "TMPDIR",
        "TMP",
        "TEMP",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_TERMINAL_PROMPT",
        "GIT_ALLOW_PROTOCOL",
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "GIT_AUTHOR_DATE",
        "GIT_COMMITTER_DATE",
        "PYTHONPATH",
    )
    git_state_names = (
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
        "GIT_DIR",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_GRAFTS_FILE",
        "GIT_INDEX_FILE",
        "GIT_INDEX_VERSION",
        "GIT_NAMESPACE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_REPLACE_REF_BASE",
        "GIT_SHALLOW_FILE",
        "GIT_WORK_TREE",
    )
    git_execution_names = (
        "GIT_ATTR_NOSYSTEM",
        "GIT_CLONE_PROTECTION_ACTIVE",
        "GIT_CONFIG_SYSTEM",
        "GIT_DEFAULT_BRANCH",
        "GIT_DEFAULT_HASH",
        "GIT_DEFAULT_REF_FORMAT",
        "GIT_EDITOR",
        "GIT_EXEC_PATH",
        "GIT_EXTERNAL_DIFF",
        "GIT_PAGER",
        "GIT_PROTOCOL",
        "GIT_PROTOCOL_FROM_USER",
        "GIT_SEQUENCE_EDITOR",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_SSH_VARIANT",
        "GIT_SSL_CAINFO",
        "GIT_SSL_CAPATH",
        "GIT_SSL_CERT",
        "GIT_SSL_KEY",
        "GIT_SSL_NO_VERIFY",
        "GIT_TEMPLATE_DIR",
        "SSH_ASKPASS_REQUIRE",
    )
    ambient_credential_names = (
        "SSH_AGENT_PID",
        "SSH_AUTH_SOCK",
        "SYSTEM_ACCESSTOKEN",
    )
    child_runtime_injection_names = (
        "BASH_ENV",
        "CORECLR_ENABLE_PROFILING",
        "CORECLR_PROFILER",
        "CORECLR_PROFILER_PATH",
        "CORECLR_PROFILER_PATH_32",
        "CORECLR_PROFILER_PATH_64",
        "DOTNET_ADDITIONAL_DEPS",
        "DOTNET_STARTUP_HOOKS",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "ENV",
        "JAVA_TOOL_OPTIONS",
        "JDK_JAVA_OPTIONS",
        "LD_AUDIT",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "NODE_OPTIONS",
        "NODE_PATH",
        "PERL5LIB",
        "PERL5OPT",
        "PYTHONBREAKPOINT",
        "PYTHONHOME",
        "PYTHONINSPECT",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
        "RUBYLIB",
        "RUBYOPT",
        "ZDOTDIR",
        "_JAVA_OPTIONS",
    )
    tool_home_names = (
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "COPILOT_HOME",
        "HERMES_HOME",
    )
    proxy_names = (
        "ALL_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
    )
    protected_exact_names = frozenset(
        (
            *exact_secret_names,
            *credential_pattern_names,
            *pinned_names,
            *security_control_names,
            *git_state_names,
            *git_execution_names,
            *ambient_credential_names,
            *child_runtime_injection_names,
            *tool_home_names,
            *proxy_names,
        )
    )
    ambient_environment = dict(os.environ)
    ambient_environment.update({name: "ambient" for name in protected_exact_names})
    for name in pinned_names:
        ambient_environment[name] = "canonical-poison"
        ambient_environment[name.lower()] = "lower-poison"
        ambient_environment[name.title()] = "mixed-poison"
    ambient_environment.update(
        {
            "APM_ARBITRARY_INHERITED_SWITCH": "ambient",
            "APM_NO_CACHE": "1",
            "APM_REGISTRY_PASS_INTERNAL": "ambient",
            "APM_REGISTRY_TOKEN_INTERNAL": "ambient",
            "APM_REGISTRY_USER_INTERNAL": "ambient",
            "CLAUDE_CONFIG_DIR": "/ambient/claude",
            "CODEX_HOME": "/ambient/codex",
            "COPILOT_HOME": "/ambient/copilot",
            "GITHUB_APM_PAT_ACME": "ambient",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "ambient",
            "GIT_DIR": "/ambient/git",
            "GIT_TRACE2_EVENT": "/ambient/git-trace.json",
            "GIT_WORK_TREE": "/ambient/work-tree",
            "HERMES_HOME": "/ambient/hermes",
            "HTTPS_PROXY": "https://ambient.invalid",
            "Git_Config_Value_1": "ambient",
            "Git_Terminal_Prompt": "ambient",
            "apm_policy_disable": "1",
            "github_token": "ambient",
            "git_config_key_1": "credential.helper",
            "git_trace2_event": "/ambient/lower-git-trace.json",
            "home": "/ambient/home",
        }
    )
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "scenario",
        base_env=ambient_environment,
    )
    environment = isolated.subprocess_env()
    case_variant_credential_names = ("GIT_TOKEN", "OPENAI_API_KEY", "NVIDIA_INFERENCE_KEY")
    case_variant_isolated = IsolatedApmEnvironment.create(
        tmp_path / "case-variant-credentials",
        base_env={name.title(): "ambient" for name in case_variant_credential_names},
    )
    case_variant_environment_names = {
        name.upper() for name in case_variant_isolated.process_environment
    }
    assert {name.upper() for name in case_variant_credential_names}.isdisjoint(
        case_variant_environment_names
    )

    stripped_names = (
        *exact_secret_names,
        *credential_pattern_names,
        *security_control_names,
        *git_state_names,
        *git_execution_names,
        *ambient_credential_names,
        *child_runtime_injection_names,
        *tool_home_names,
        *proxy_names,
        "APM_ARBITRARY_INHERITED_SWITCH",
        "APM_NO_CACHE",
        "APM_REGISTRY_PASS_INTERNAL",
        "APM_REGISTRY_TOKEN_INTERNAL",
        "APM_REGISTRY_USER_INTERNAL",
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "COPILOT_HOME",
        "GITHUB_APM_PAT_ACME",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "GIT_TRACE2_EVENT",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "HERMES_HOME",
        "HTTPS_PROXY",
        "Git_Config_Value_1",
        "apm_policy_disable",
        "github_token",
        "git_config_key_1",
    )
    normalized_environment_names = {name.upper() for name in environment}
    assert {name.upper() for name in stripped_names}.isdisjoint(normalized_environment_names)
    pinned_environment = {
        "HOME": str(isolated.home),
        "USERPROFILE": str(isolated.home),
        "XDG_CONFIG_HOME": str(isolated.root / "xdg-config"),
        "XDG_CACHE_HOME": str(isolated.root / "xdg-cache"),
        "LOCALAPPDATA": str(isolated.root / "local-app-data"),
        "APM_HOME": str(isolated.config_root),
        "APM_CACHE_DIR": str(isolated.cache_root),
        "APM_TEMP_DIR": str(isolated.temp_root),
        "GH_CONFIG_DIR": str(isolated.root / "gh-config"),
        "AZURE_CONFIG_DIR": str(isolated.root / "azure-config"),
        "TMPDIR": str(isolated.temp_root),
        "TMP": str(isolated.temp_root),
        "TEMP": str(isolated.temp_root),
        "GIT_CONFIG_GLOBAL": str(isolated.root / "gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "file",
        "GIT_AUTHOR_NAME": "APM Test",
        "GIT_AUTHOR_EMAIL": "apm-test@example.invalid",
        "GIT_COMMITTER_NAME": "APM Test",
        "GIT_COMMITTER_EMAIL": "apm-test@example.invalid",
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
        "PYTHONPATH": str(isolated.root / "network_guard"),
    }
    assert len(environment) == len(normalized_environment_names)
    assert "APM_POLICY_DISABLE" not in environment
    assert "APM_ALLOW_PROTOCOL_FALLBACK" not in environment
    assert "APM_GIT_PROTOCOL" not in environment
    assert "SSH_AUTH_SOCK" not in environment
    assert "SSH_AGENT_PID" not in environment
    assert "SYSTEM_ACCESSTOKEN" not in environment
    assert "git_terminal_prompt" not in environment
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    for name, expected_value in pinned_environment.items():
        matching_names = [actual_name for actual_name in environment if actual_name.upper() == name]
        assert matching_names == [name]
        assert environment[name] == expected_value
    for name in ("GH_CONFIG_DIR", "AZURE_CONFIG_DIR", "GIT_CONFIG_GLOBAL"):
        assert Path(environment[name]).resolve().is_relative_to(isolated.root.resolve())

    exact_names = ("HOME", "GIT_ALLOW_PROTOCOL", "GITHUB_TOKEN", "PYTHONPATH")
    dynamic_prefix_names = (
        "GITHUB_APM_PAT_ACME",
        "APM_REGISTRY_TOKEN_INTERNAL",
        "APM_REGISTRY_USER_INTERNAL",
        "APM_REGISTRY_PASS_INTERNAL",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "GIT_TRACE",
        "GIT_TRACE_PACKET",
        "GIT_TRACE2",
        "GIT_TRACE2_EVENT",
        "Git_Trace2_Event",
    )
    case_variant_exact_names = ("home", "github_token", "Git_Allow_Protocol")
    for name in exact_names + case_variant_exact_names:
        with pytest.raises(ValueError, match="protected environment"):
            isolated.subprocess_env(overrides={name: "unsafe"})
    assert tuple(pinned_environment) == pinned_names
    for name in protected_exact_names:
        for variant in (name, name.title()):
            with pytest.raises(ValueError, match="protected environment"):
                isolated.subprocess_env(overrides={variant: "unsafe"})
    for name in dynamic_prefix_names:
        for variant in (name, name.lower()):
            with pytest.raises(ValueError, match="protected environment"):
                isolated.subprocess_env(overrides={variant: "unsafe"})

    safe_environment = isolated.subprocess_env(
        overrides={
            "APM_LOG_LEVEL": "debug",
            "APM_NO_CACHE": "1",
            "MODEL_KEY_FORMAT": "safe",
            "PASSWORD_POLICY": "safe",
            "TOKEN_COUNT": "1",
            "SCENARIO": "safe",
        }
    )
    assert safe_environment["APM_LOG_LEVEL"] == "debug"
    assert safe_environment["APM_NO_CACHE"] == "1"
    assert safe_environment["MODEL_KEY_FORMAT"] == "safe"
    assert safe_environment["PASSWORD_POLICY"] == "safe"
    assert safe_environment["TOKEN_COUNT"] == "1"
    assert safe_environment["SCENARIO"] == "safe"


def test_python_child_network_is_denied(tmp_path: Path) -> None:
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "scenario",
        base_env=os.environ,
    )

    local_result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os; "
                "read_fd, write_fd = os.pipe(); "
                "os.write(write_fd, b'x'); "
                "assert os.read(read_fd, 1) == b'x'; "
                "os.close(read_fd); os.close(write_fd)"
            ),
        ],
        cwd=isolated.work_root,
        env=isolated.subprocess_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert local_result.returncode == 0, local_result.stderr

    local_socket_result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os, socket, sys; "
                "hasattr(socket, 'AF_UNIX') or sys.exit(0); "
                "path = 'local.sock'; "
                "server = socket.SocketType(socket.AF_UNIX, socket.SOCK_STREAM); "
                "server.bind(path); server.listen(); "
                "client = socket.SocketType(socket.AF_UNIX, socket.SOCK_STREAM); "
                "client.connect(path); accepted, _ = server.accept(); "
                "client.sendall(b'x'); assert accepted.recv(1) == b'x'; "
                "accepted.close(); client.close(); server.close(); os.unlink(path)"
            ),
        ],
        cwd=isolated.work_root,
        env=isolated.subprocess_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert local_socket_result.returncode == 0, local_socket_result.stderr

    direct_local_socket_result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import _socket, os, sys; "
                "hasattr(_socket, 'AF_UNIX') or sys.exit(0); "
                "path = 'direct-local.sock'; "
                "server = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM); "
                "server.bind(path); server.listen(); "
                "client = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM); "
                "client.connect(path); "
                "client.close(); server.close(); os.unlink(path)"
            ),
        ],
        cwd=isolated.work_root,
        env=isolated.subprocess_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert direct_local_socket_result.returncode == 0, direct_local_socket_result.stderr

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
            "socket.SocketType(socket.AF_INET, socket.SOCK_STREAM)"
            ".connect(('203.0.113.1', 443))"
        ),
        (
            "import socket; "
            "socket.SocketType(socket.AF_INET6, socket.SOCK_STREAM)"
            ".connect(('2001:db8::1', 443))"
        ),
        "import socket; socket.gethostbyname('example.invalid')",
        "import socket; socket.gethostbyname_ex('example.invalid')",
        "import socket; socket.gethostbyaddr('203.0.113.1')",
        ("import socket; socket.getnameinfo(('203.0.113.1', 443), socket.NI_NUMERICHOST)"),
        ("import socket; socket.getnameinfo(('2001:db8::1', 443), socket.NI_NUMERICHOST)"),
        (
            "import _socket; "
            "_socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)"
            ".bind(('127.0.0.1', 0))"
        ),
        ("import _socket; _socket.socket(_socket.AF_INET6, _socket.SOCK_STREAM).bind(('::1', 0))"),
        (
            "import _socket; "
            "_socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)"
            ".connect(('203.0.113.1', 443))"
        ),
        (
            "import _socket; "
            "_socket.socket(_socket.AF_INET6, _socket.SOCK_STREAM)"
            ".connect(('2001:db8::1', 443))"
        ),
        (
            "import socket; "
            "socket.socket(socket.AF_INET, socket.SOCK_STREAM)"
            ".connect_ex(('203.0.113.1', 443))"
        ),
        (
            "import socket; "
            "socket.socket(socket.AF_INET6, socket.SOCK_STREAM)"
            ".connect_ex(('2001:db8::1', 443))"
        ),
        ("import socket; socket.socket(socket.AF_INET, socket.SOCK_STREAM).bind(('127.0.0.1', 0))"),
        ("import socket; socket.socket(socket.AF_INET6, socket.SOCK_STREAM).bind(('::1', 0))"),
        (
            "import socket; socket.SocketType(socket.AF_INET, socket.SOCK_STREAM).bind(('127.0.0.1', 0))"
        ),
        ("import socket; socket.SocketType(socket.AF_INET6, socket.SOCK_STREAM).bind(('::1', 0))"),
        ("import socket; socket.socket(socket.AF_INET, socket.SOCK_STREAM).listen()"),
        ("import socket; socket.socket(socket.AF_INET6, socket.SOCK_STREAM).listen()"),
        ("import socket; socket.socket(socket.AF_INET, socket.SOCK_STREAM).accept()"),
        ("import socket; socket.socket(socket.AF_INET6, socket.SOCK_STREAM).accept()"),
        ("import socket; socket.socket(socket.AF_INET, socket.SOCK_STREAM)._accept()"),
        ("import socket; socket.socket(socket.AF_INET6, socket.SOCK_STREAM)._accept()"),
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
        (
            "import socket; "
            "socket.socket(socket.AF_INET, socket.SOCK_DGRAM)"
            ".sendmsg([b'x'], [], 0, ('203.0.113.1', 9))"
        ),
        (
            "import socket; "
            "socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)"
            ".sendmsg([b'x'], [], 0, ('2001:db8::1', 9))"
        ),
        "import socket; socket.getaddrinfo('example.invalid', 443)",
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
    poisoned_template = tmp_path / "poisoned-template"
    poisoned_hook = poisoned_template / "hooks" / "ambient-hook"
    poisoned_hook.parent.mkdir(parents=True)
    poisoned_hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    base_env = dict(os.environ)
    base_env.update(
        {
            "GIT_TEMPLATE_DIR": str(poisoned_template),
            "git_template_dir": str(poisoned_template),
            "Git_Template_Dir": str(poisoned_template),
        }
    )
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "scenario",
        base_env=base_env,
    )

    bare_repository = isolated.repository_root / "allowed.git"
    init_result = subprocess.run(
        ["git", "init", "--bare", str(bare_repository)],
        cwd=isolated.work_root,
        env=isolated.subprocess_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert init_result.returncode == 0, init_result.stderr
    assert not (bare_repository / "hooks" / poisoned_hook.name).exists()

    file_result = subprocess.run(
        ["git", "ls-remote", bare_repository.as_uri(), "HEAD"],
        cwd=isolated.work_root,
        env=isolated.subprocess_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert file_result.returncode == 0, file_result.stderr

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
