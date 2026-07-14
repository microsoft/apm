"""Hermetic process environments for APM lifecycle integration tests."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

_NETWORK_GUARD = """\
import socket

_MESSAGE = "IP network disabled by test environment"
_REAL_SOCKET = socket.socket


class _GuardedSocket(_REAL_SOCKET):
    def _accept(self):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise OSError(_MESSAGE)
        return super()._accept()

    def bind(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise OSError(_MESSAGE)
        return super().bind(address)

    def connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise OSError(_MESSAGE)
        return super().connect(address)

    def connect_ex(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise OSError(_MESSAGE)
        return super().connect_ex(address)

    def listen(self, backlog=0):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise OSError(_MESSAGE)
        return super().listen(backlog)

    def accept(self):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise OSError(_MESSAGE)
        return super().accept()

    def sendto(self, *args, **kwargs):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise OSError(_MESSAGE)
        return super().sendto(*args, **kwargs)

    def sendmsg(self, *args, **kwargs):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise OSError(_MESSAGE)
        return super().sendmsg(*args, **kwargs)


def _deny_network(*args, **kwargs):
    raise OSError(_MESSAGE)


socket.socket = _GuardedSocket
socket.create_connection = _deny_network
socket.getaddrinfo = _deny_network
"""

_SECRET_ENV_NAMES = frozenset(
    {
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
        "ACTIONS_ID_TOKEN_REQUEST_URL",
        "ACTIONS_RUNTIME_TOKEN",
        "ADO_APM_PAT",
        "ARTIFACTORY_APM_TOKEN",
        "AZURE_DEVOPS_EXT_PAT",
        "COPILOT_GITHUB_TOKEN",
        "GH_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_APM_PAT",
        "GITHUB_COPILOT_PAT",
        "GITHUB_ENTERPRISE_TOKEN",
        "GITHUB_MODELS_KEY",
        "GITHUB_PERSONAL_ACCESS_TOKEN",
        "GITHUB_TOKEN",
        "GITLAB_APM_PAT",
        "GITLAB_TOKEN",
        "GIT_ASKPASS",
        "PROXY_REGISTRY_TOKEN",
        "SSH_ASKPASS",
    }
)
_SECRET_ENV_PREFIXES = (
    "APM_REGISTRY_PASS_",
    "APM_REGISTRY_TOKEN_",
    "APM_REGISTRY_USER_",
    "GITHUB_APM_PAT_",
)
_CREDENTIAL_STORE_ENV_NAMES = frozenset(
    {
        "AZURE_CONFIG_DIR",
        "GH_CONFIG_DIR",
    }
)
_TOOL_HOME_ENV_NAMES = frozenset(
    {
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "COPILOT_HOME",
        "HERMES_HOME",
    }
)
_GIT_STATE_ENV_NAMES = frozenset(
    {
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
    }
)
_GIT_CONFIG_INJECTION_PREFIXES = (
    "GIT_CONFIG_KEY_",
    "GIT_CONFIG_VALUE_",
)
_PROXY_ENV_NAMES = frozenset(
    {
        "ALL_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)
_STRIPPED_ENV_NAMES = (
    _SECRET_ENV_NAMES
    | _PROXY_ENV_NAMES
    | _GIT_STATE_ENV_NAMES
    | _CREDENTIAL_STORE_ENV_NAMES
    | _TOOL_HOME_ENV_NAMES
)
_STRIPPED_ENV_PREFIXES = _SECRET_ENV_PREFIXES + _GIT_CONFIG_INJECTION_PREFIXES
_PINNED_ENVIRONMENT_NAMES = (
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
_SECURITY_CONTROL_ENV_NAMES = frozenset(
    {
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
        "CURL_CA_BUNDLE",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_FILE",
    }
)
_PROTECTED_OVERRIDE_NAMES = (
    frozenset(_PINNED_ENVIRONMENT_NAMES) | _STRIPPED_ENV_NAMES | _SECURITY_CONTROL_ENV_NAMES
)


def _normalized_env_name(name: str) -> str:
    """Normalize an environment name using Windows-compatible semantics."""
    return name.upper()


def _is_protected_override(name: str) -> bool:
    normalized_name = _normalized_env_name(name)
    return normalized_name in _PROTECTED_OVERRIDE_NAMES or normalized_name.startswith(
        _STRIPPED_ENV_PREFIXES
    )


def _should_strip_inherited(name: str) -> bool:
    normalized_name = _normalized_env_name(name)
    return (
        normalized_name.startswith("APM_")
        or normalized_name in _PROTECTED_OVERRIDE_NAMES
        or normalized_name.startswith(_STRIPPED_ENV_PREFIXES)
    )


def _deduplicate_environment(base_env: Mapping[str, str]) -> dict[str, str]:
    environment: dict[str, str] = {}
    spellings: dict[str, str] = {}
    for name, value in base_env.items():
        normalized_name = _normalized_env_name(name)
        existing_name = spellings.get(normalized_name)
        if existing_name is None:
            environment[name] = value
            spellings[normalized_name] = name
        elif name == normalized_name and existing_name != name:
            environment.pop(existing_name)
            environment[name] = value
            spellings[normalized_name] = name
    return environment


def _set_environment_value(
    environment: dict[str, str],
    name: str,
    value: str,
) -> None:
    normalized_name = _normalized_env_name(name)
    for existing_name in tuple(environment):
        if _normalized_env_name(existing_name) == normalized_name:
            environment.pop(existing_name)
    environment[name] = value


@dataclass(frozen=True)
class IsolatedApmEnvironment:
    """Filesystem roots and immutable child environment for one test scenario."""

    root: Path
    home: Path
    config_root: Path
    cache_root: Path
    package_root: Path
    repository_root: Path
    work_root: Path
    temp_root: Path
    process_environment: Mapping[str, str]

    @classmethod
    def create(
        cls,
        root: Path,
        *,
        base_env: Mapping[str, str],
    ) -> IsolatedApmEnvironment:
        """Create unique scenario roots and a sanitized process environment."""
        root.mkdir(parents=True, exist_ok=False)
        home = root / "home"
        config_root = home / ".apm"
        cache_root = root / "cache"
        package_root = root / "packages"
        repository_root = root / "repositories"
        work_root = root / "work"
        temp_root = root / "tmp"
        guard_root = root / "network_guard"
        xdg_config_root = root / "xdg-config"
        xdg_cache_root = root / "xdg-cache"
        local_app_data = root / "local-app-data"
        gh_config_root = root / "gh-config"
        azure_config_root = root / "azure-config"
        for directory in (
            home,
            config_root,
            cache_root,
            package_root,
            repository_root,
            work_root,
            temp_root,
            guard_root,
            xdg_config_root,
            xdg_cache_root,
            local_app_data,
            gh_config_root,
            azure_config_root,
        ):
            directory.mkdir(parents=True)

        git_config = root / "gitconfig"
        git_config.write_text(
            '[protocol "file"]\n\tallow = always\n',
            encoding="utf-8",
        )
        (guard_root / "sitecustomize.py").write_text(
            _NETWORK_GUARD,
            encoding="utf-8",
        )

        environment = _deduplicate_environment(base_env)
        for name in list(environment):
            if _should_strip_inherited(name):
                environment.pop(name, None)

        pinned_environment = {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "XDG_CONFIG_HOME": str(xdg_config_root),
            "XDG_CACHE_HOME": str(xdg_cache_root),
            "LOCALAPPDATA": str(local_app_data),
            "APM_HOME": str(config_root),
            "APM_CACHE_DIR": str(cache_root),
            "APM_TEMP_DIR": str(temp_root),
            "GH_CONFIG_DIR": str(gh_config_root),
            "AZURE_CONFIG_DIR": str(azure_config_root),
            "TMPDIR": str(temp_root),
            "TMP": str(temp_root),
            "TEMP": str(temp_root),
            "GIT_CONFIG_GLOBAL": str(git_config),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ALLOW_PROTOCOL": "file",
            "GIT_AUTHOR_NAME": "APM Test",
            "GIT_AUTHOR_EMAIL": "apm-test@example.invalid",
            "GIT_COMMITTER_NAME": "APM Test",
            "GIT_COMMITTER_EMAIL": "apm-test@example.invalid",
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
            "PYTHONPATH": str(guard_root),
        }
        if tuple(pinned_environment) != _PINNED_ENVIRONMENT_NAMES:
            raise AssertionError("Pinned environment names are out of sync")
        for name, value in pinned_environment.items():
            _set_environment_value(environment, name, value)
        return cls(
            root=root,
            home=home,
            config_root=config_root,
            cache_root=cache_root,
            package_root=package_root,
            repository_root=repository_root,
            work_root=work_root,
            temp_root=temp_root,
            process_environment=MappingProxyType(environment),
        )

    def subprocess_env(
        self,
        *,
        overrides: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        """Return a child environment with only non-protected overrides applied."""
        environment = dict(self.process_environment)
        if overrides:
            protected = {name for name in overrides if _is_protected_override(name)}
            if protected:
                names = ", ".join(sorted(protected))
                raise ValueError(f"Cannot override protected environment variables: {names}")
            for name, value in overrides.items():
                _set_environment_value(environment, name, value)
        return environment
