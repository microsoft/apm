"""Cached git binary lookup and subprocess environment sanitization.

Ensures that APM's git subprocess calls use a clean environment free
of ambient git state variables that could bias operations (e.g. when
APM is invoked from within a git repository's hook or worktree).

Preserved variables (user-controlled config for proxy/auth):
- GIT_SSH, GIT_SSH_COMMAND, GIT_ASKPASS, SSH_ASKPASS
- GIT_HTTP_USER_AGENT, GIT_TERMINAL_PROMPT
- GIT_CONFIG_GLOBAL, GIT_CONFIG_SYSTEM

Git state variables stripped after external-process sanitization:
- GIT_DIR, GIT_CONFIG, GIT_WORK_TREE, GIT_INDEX_FILE
- GIT_OBJECT_DIRECTORY, GIT_ALTERNATE_OBJECT_DIRECTORIES
- GIT_COMMON_DIR, GIT_NAMESPACE, GIT_INDEX_VERSION
- GIT_CEILING_DIRECTORIES, GIT_DISCOVERY_ACROSS_FILESYSTEM
- GIT_REPLACE_REF_BASE, GIT_GRAFT_FILE, GIT_SHALLOW_FILE
- GIT_IMPLICIT_WORK_TREE, GIT_NO_REPLACE_OBJECTS, GIT_PREFIX
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import SplitResult, urlsplit, urlunsplit

from apm_cli.utils.subprocess_env import external_process_env

# Module-level cached git executable path (successful resolutions only).
_git_executable: str | None = None
_gh_executable: str | None = None
_git_init_run = subprocess.run

# Variables that represent ambient git state -- strip these to avoid
# biasing APM's git operations when invoked from within another repo
# or when the calling environment uses git's discovery / replacement
# / grafts overrides.
_STRIP_GIT_VARS: frozenset[str] = frozenset(
    {
        "GIT_DIR",
        "GIT_CONFIG",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
        "GIT_INDEX_VERSION",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_REPLACE_REF_BASE",
        "GIT_GRAFT_FILE",
        "GIT_SHALLOW_FILE",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_PREFIX",
    }
)
_GIT_CHILD_TOKEN_ENV_NAMES = frozenset(
    {
        "ADO_APM_PAT",
        "ARTIFACTORY_APM_TOKEN",
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
        "PROXY_REGISTRY_TOKEN",
    }
)
_GIT_CHILD_TOKEN_ENV_PREFIXES = (
    "APM_REGISTRY_PASS_",
    "APM_REGISTRY_TOKEN_",
    "APM_REGISTRY_USER_",
    "GITHUB_APM_PAT_",
)
_MANAGED_GIT_AUTH_INTENT_ENV = "APM_GIT_MANAGED_AUTH_INTENT"
_AUTH_HEADER_RE = re.compile(r"(?im)(authorization:\s*)[^\r\n]+")
_DIAGNOSTIC_GIT_URL_RE = re.compile(r"(?i)\b(?:https?|ssh|git)://[^\s'\"<>]+")
_URL_USERINFO_RE = re.compile(r"(https?://)[^/@\s]+@")
_URL_SECRET_QUERY_RE = re.compile(
    r"(?i)([?&](?:access_token|auth|key|password|secret|token)=)[^&#\s]+"
)
_SECRET_ENV_ASSIGNMENT_RE = re.compile(
    r"(?i)\b("
    r"ADO_APM_PAT|GH_TOKEN|GITHUB_APM_PAT(?:_[A-Z0-9_]+)?|GITHUB_TOKEN|"
    r"GITLAB_APM_PAT|GITLAB_TOKEN"
    r")=[^\s]+"
)
_BARE_PLATFORM_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    r"github_pat_[A-Za-z0-9_]{20,}|"
    r"gh[oprsu]_[A-Za-z0-9_]{6,}|"
    r"gl(?:agent|cbt|ft|pat|ptt|rt|soat)[-_][A-Za-z0-9_-]{6,}|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}|"
    r"[A-Za-z0-9]{75}AZDO[A-Za-z0-9]{5}|"
    r"[A-Za-z0-9]{52}"
    r")(?![A-Za-z0-9_])"
)
_LABELLED_SECRET_RE = re.compile(
    r"(?i)\b(token|password|secret|credential)"
    r"(\s*(?:[:=]\s*|\s+))"
    r"([A-Za-z0-9_.~+/-]{4,})"
)
_SSH_KEY_PATH_RE = re.compile(
    r"(?i)((?:enter passphrase for key|identity file)\s+)(['\"]?)[^'\"\r\n]+(['\"]?)"
)
_SENSITIVE_HTTP_HEADER_PARTS = frozenset(
    {
        "auth",
        "authorization",
        "cookie",
        "credential",
        "key",
        "password",
        "secret",
        "token",
    }
)
_REMOTE_HELPER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*::")
_SCP_HOST_RE = re.compile(r"^(?:[^/@:\s]+@)?(\[[^\]]+\]|[^/:@\s]+):")
_HTTP_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_GIT_CONFIG_PROBE_TIMEOUTS = (10, 30)

_URL_REWRITE_RECOVERY = (
    "inspect matching rules with "
    "'git config --show-origin --get-regexp ^url\\..*\\.insteadOf$' "
    "and remove the unsafe rule"
)


class GitUrlRewriteError(ValueError):
    """A stable, actionable rejection of an unsafe Git URL rewrite."""

    def __init__(self, reason: str, message: str) -> None:
        """Initialize one rejection with a machine-readable reason."""
        self.reason = reason
        self.recovery_hint = _URL_REWRITE_RECOVERY
        super().__init__(f"{message}; {_URL_REWRITE_RECOVERY}")


class GitUrlRewriteProbeError(ValueError):
    """An actionable failure to inspect effective Git configuration."""

    def __init__(self, category: str) -> None:
        """Initialize a safe probe failure without rendering raw Git output."""
        self.category = category
        super().__init__(
            f"Unable to verify Git URL rewrite safety ({category}); "
            f"check Git configuration and retry; {_URL_REWRITE_RECOVERY}"
        )


def _run_git_config(
    command: Sequence[str],
    *,
    capture_output: bool,
    check: bool,
    cwd: str | None,
    env: dict[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[bytes]:
    """Run config inspection independently from mocked network subprocesses."""
    del capture_output, check
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


_git_config_run = _run_git_config


def _run_git_config_probe(
    command: Sequence[str],
    *,
    capture_output: bool,
    check: bool,
    cwd: str | None,
    env: dict[str, str],
    timeout_category: str,
) -> subprocess.CompletedProcess[bytes]:
    """Run a bounded Git config probe with one retry for slow CI runners."""
    for index, timeout in enumerate(_GIT_CONFIG_PROBE_TIMEOUTS):
        try:
            return _git_config_run(
                command,
                capture_output=capture_output,
                check=check,
                cwd=cwd,
                env=env,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            if index == len(_GIT_CONFIG_PROBE_TIMEOUTS) - 1:
                raise GitUrlRewriteProbeError(timeout_category) from exc
    raise GitUrlRewriteProbeError(timeout_category)


def _executable_exclusion_root() -> Path:
    """Return the nearest repository or APM project root containing the cwd."""
    cwd = Path.cwd().resolve()
    ancestors = (cwd, *cwd.parents)
    for directory in ancestors:
        if (directory / ".git").exists():
            return directory
    for directory in ancestors:
        if (directory / "apm.yml").is_file():
            return directory
    return cwd


def _resolve_trusted_executable(name: str) -> str:
    """Resolve an executable from PATH directories outside the project."""
    exclusion_root = _executable_exclusion_root()
    for entry in os.get_exec_path():
        if not entry:
            continue
        directory = Path(entry).expanduser().resolve()
        try:
            if directory == exclusion_root or directory.is_relative_to(exclusion_root):
                continue
        except (OSError, ValueError):
            continue
        candidate = shutil.which(str(directory / name))
        if candidate is None:
            continue
        resolved = Path(candidate).resolve()
        try:
            if resolved == exclusion_root or resolved.is_relative_to(exclusion_root):
                continue
        except (OSError, ValueError):
            continue
        return str(resolved)
    raise FileNotFoundError(f"{name} executable not found on trusted PATH directories")


def get_git_executable() -> str:
    """Return the path to the git executable (cached after a successful lookup).

    Resolves explicit PATH entries and excludes the current working tree.
    Failed lookups are not cached because PATH can change within a
    long-lived process.

    Returns:
        Absolute or relative path to the git binary.

    Raises:
        FileNotFoundError: If git is not found on PATH.
    """
    global _git_executable
    if _git_executable is not None:
        return _git_executable

    try:
        _git_executable = _resolve_trusted_executable("git")
    except FileNotFoundError:
        raise FileNotFoundError(  # noqa: B904
            "git executable not found on PATH. Please install git: https://git-scm.com/downloads"
        )
    return _git_executable


def get_gh_executable() -> str:
    """Return a trusted absolute path to the GitHub CLI executable."""
    global _gh_executable
    if _gh_executable is None:
        try:
            _gh_executable = _resolve_trusted_executable("gh")
        except FileNotFoundError:
            raise FileNotFoundError(  # noqa: B904
                "GitHub CLI executable not found on PATH. "
                "Please install it: https://cli.github.com/"
            )
    return _gh_executable


def git_subprocess_env(overrides: dict[str, object] | None = None) -> dict[str, str]:
    """Return a sanitized environment dict for git subprocesses.

    Restores PyInstaller-managed dynamic-library variables first, then
    strips ambient git state variables while preserving user-controlled
    configuration (proxy, auth, SSH settings). Optional overrides are
    applied through the same state-variable filter.

    Returns:
        An external-process-safe copy of ``os.environ`` with problematic
        git variables removed.
    """
    base = (
        None
        if overrides is None
        else {key: value for key, value in overrides.items() if isinstance(value, str)}
    )
    env = {
        key: value
        for key, value in external_process_env(base).items()
        if key not in _STRIP_GIT_VARS
    }
    env["GIT_TRACE_REDACT"] = "1"
    return env


def redact_git_diagnostic(text: str) -> str:
    """Redact credentials and private key paths from Git diagnostics."""
    without_url_secrets = _DIAGNOSTIC_GIT_URL_RE.sub(_redact_git_diagnostic_url, text)
    without_userinfo = _URL_USERINFO_RE.sub(r"\1***@", without_url_secrets)
    without_query_secrets = _URL_SECRET_QUERY_RE.sub(r"\1***", without_userinfo)
    without_headers = _AUTH_HEADER_RE.sub(r"\1******", without_query_secrets)
    without_env = _SECRET_ENV_ASSIGNMENT_RE.sub(r"\1=***", without_headers)
    without_tokens = _BARE_PLATFORM_TOKEN_RE.sub("***", without_env)
    without_labelled = _LABELLED_SECRET_RE.sub(r"\1\2***", without_tokens)
    return _SSH_KEY_PATH_RE.sub(r"\1'[REDACTED]'", without_labelled)


def _redact_git_diagnostic_url(match: re.Match[str]) -> str:
    """Remove userinfo/fragment data and redact query values in one URL."""
    try:
        parsed = urlsplit(match.group(0))
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        if parsed.username is not None or parsed.password is not None:
            host = f"***@{host}"
        query = re.sub(r"(^|&)([^=&]+)=[^&]*", r"\1\2=***", parsed.query)
        if parsed.query and query == parsed.query and "=" not in parsed.query:
            query = "***"
        return urlunsplit((parsed.scheme, host, parsed.path, query, ""))
    except ValueError:
        return "<redacted-git-url>"


def _is_credential_bearing_http_header(value: str) -> bool:
    """Return whether one http.extraHeader value can carry a credential."""
    if not _is_valid_http_extraheader_value(value):
        return False
    stripped = value.strip()
    if not stripped:
        return False
    name, separator, _header_value = stripped.partition(":")
    if not separator:
        return True
    if _BARE_PLATFORM_TOKEN_RE.search(_header_value):
        return True
    if re.match(r"(?i)^\s*(?:basic|bearer|token)\s+\S+", _header_value):
        return True
    parts = frozenset(part for part in re.split(r"[^a-z0-9]+", name.lower()) if part)
    return bool(parts & _SENSITIVE_HTTP_HEADER_PARTS)


def _is_valid_http_extraheader_value(value: str) -> bool:
    """Return whether one ambient value is a single valid HTTP header line."""
    if not value.strip():
        return True
    if any(character in value for character in ("\r", "\n", "\0")):
        return False
    if any(
        (ord(character) < 32 and character != "\t") or ord(character) == 127 for character in value
    ):
        return False
    name, separator, _field_value = value.partition(":")
    return bool(separator and _HTTP_HEADER_NAME_RE.fullmatch(name))


def _is_http_extraheader_key(key: str) -> bool:
    """Return whether *key* is an unscoped or URL-scoped extraHeader."""
    normalized = key.lower()
    return normalized == "http.extraheader" or (
        normalized.startswith("http.") and normalized.endswith(".extraheader")
    )


def _is_credential_helper_key(key: str) -> bool:
    """Return whether *key* configures a native Git credential helper."""
    normalized = key.lower()
    return normalized == "credential.helper" or (
        normalized.startswith("credential.") and normalized.endswith(".helper")
    )


def git_subprocess_error_text(exc: BaseException) -> str:
    """Return captured Git output when a subprocess exception provides it."""
    if isinstance(exc, subprocess.CalledProcessError):
        for stream in (exc.stderr, exc.stdout):
            if isinstance(stream, bytes):
                stream = stream.decode("utf-8", errors="replace")
            if isinstance(stream, str) and stream.strip():
                return redact_git_diagnostic(stream.strip())
    return redact_git_diagnostic(str(exc))


def clear_git_platform_token_env(
    env: dict[str, str],
    *,
    remove: bool = False,
) -> None:
    """Mask or remove raw platform credential sources from a Git child."""
    for key in tuple(env):
        if key in _GIT_CHILD_TOKEN_ENV_NAMES or key.startswith(_GIT_CHILD_TOKEN_ENV_PREFIXES):
            if remove:
                env.pop(key, None)
            else:
                env[key] = ""


def clear_git_auth_env(
    env: dict[str, str],
    *,
    remove_helpers: bool = False,
) -> None:
    """Remove inherited Git authorization channels while retaining other config."""
    env.pop(_MANAGED_GIT_AUTH_INTENT_ENV, None)
    env.pop("GIT_TOKEN", None)
    env.pop("GIT_HTTP_EXTRAHEADER", None)
    env.pop("GIT_CONFIG_PARAMETERS", None)
    try:
        count = int(env.pop("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        count = 0
    retained: list[tuple[str, str]] = []
    for index in range(max(0, count)):
        key = env.pop(f"GIT_CONFIG_KEY_{index}", "")
        value = env.pop(f"GIT_CONFIG_VALUE_{index}", "")
        if (
            _is_http_extraheader_key(key)
            and (
                not _is_valid_http_extraheader_value(value)
                or not value.strip()
                or _is_credential_bearing_http_header(value)
            )
        ) or value.strip().lower().startswith("authorization:"):
            continue
        if remove_helpers and _is_credential_helper_key(key):
            continue
        if key:
            retained.append((key, value))
    for key in tuple(env):
        if key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            env.pop(key, None)
    if retained:
        env["GIT_CONFIG_COUNT"] = str(len(retained))
        for index, (key, value) in enumerate(retained):
            env[f"GIT_CONFIG_KEY_{index}"] = key
            env[f"GIT_CONFIG_VALUE_{index}"] = value


def git_no_hooks_args() -> tuple[str, str]:
    """Return the canonical per-command fence against repository Git hooks."""
    return "-c", "core.hooksPath=/dev/null"


def git_no_templates_args() -> tuple[str]:
    """Return the canonical clone fence against template-provided config."""
    return ("--template=",)


class _GitProgress(Protocol):
    """Git progress callback consumed by the clone subprocess adapter."""

    def new_message_handler(self) -> Callable[[str], None]:
        """Return a callback that parses one Git progress line."""


@dataclass(frozen=True)
class GitConfigEntry:
    """One flattened effective Git configuration entry."""

    scope: str
    key: str
    value: str


@dataclass(frozen=True)
class _GitConfigSnapshot:
    """Configuration facts validated for one Git child."""

    entries: tuple[GitConfigEntry, ...]
    rewrites: tuple[tuple[str, str], ...]
    http_headers: tuple[GitConfigEntry, ...]


@dataclass(frozen=True)
class _GitAuthFence:
    """Auth config selected by APM for one effective HTTP remote."""

    remote_url: str
    reset_headers: bool
    suppress_helpers: bool
    safe_headers: tuple[str, ...]
    managed_header: str | None


def _read_effective_git_config(
    env: dict[str, str],
    *,
    git_dir: Path | None = None,
    worktree: Path | None = None,
) -> _GitConfigSnapshot:
    """Return the flattened config visible to one Git child."""
    if git_dir is not None and worktree is not None:
        raise ValueError("git_dir and worktree are mutually exclusive")
    command = [get_git_executable()]
    probe_env = dict(env)
    probe_cwd: str | None = None
    if git_dir is not None:
        command.extend(("--git-dir", str(git_dir)))
    elif worktree is not None:
        command.extend(("-C", str(worktree)))
    else:
        # `git clone <url>` does not consume the invoking repository's local
        # config. Run from the Git executable's directory so a hook's cwd cannot
        # create false policy failures while system, global, and process-scoped
        # config remain visible.
        probe_cwd = str(Path(get_git_executable()).resolve().parent)
    command.extend(
        (
            "config",
            "--null",
            "--show-scope",
            "--list",
        )
    )
    try:
        result = _run_git_config_probe(
            command,
            capture_output=True,
            check=False,
            cwd=probe_cwd,
            env=probe_env,
            timeout_category="Git config probe timed out",
        )
    except OSError as exc:
        raise GitUrlRewriteProbeError("Git config probe could not start") from exc
    if result.returncode != 0 or not isinstance(result.stdout, bytes):
        raise GitUrlRewriteProbeError("Git config probe failed")

    fields = tuple(field for field in result.stdout.split(b"\0") if field)
    if len(fields) % 2:
        raise GitUrlRewriteProbeError("Git config returned malformed output")

    entries: list[GitConfigEntry] = []
    rewrites: list[tuple[str, str]] = []
    http_headers: list[GitConfigEntry] = []
    for index in range(0, len(fields), 2):
        scope, payload = fields[index : index + 2]
        if b"\n" not in payload:
            raise GitUrlRewriteProbeError("Git config returned malformed output")
        key, value = payload.split(b"\n", 1)
        try:
            scope_text = scope.decode("utf-8")
            key_text = key.decode("utf-8")
            value_text = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GitUrlRewriteProbeError("Git config returned non-UTF-8 output") from exc
        config_entry = GitConfigEntry(scope_text, key_text, value_text)
        entries.append(config_entry)
        normalized = key_text.lower()
        if normalized.startswith("http.") or normalized == "http.extraheader":
            if normalized.endswith(".extraheader") and _is_valid_http_extraheader_value(value_text):
                http_headers.append(config_entry)
            continue
        if not normalized.startswith("url.") or not normalized.endswith(".insteadof"):
            continue
        replacement = key_text[4 : -len(".insteadOf")]
        if not replacement or not value_text:
            raise GitUrlRewriteProbeError("Git config returned an incomplete rewrite")
        rewrites.append((replacement, value_text))
    return _GitConfigSnapshot(tuple(entries), tuple(rewrites), tuple(http_headers))


def _has_applicable_http_authorization(
    remote_url: str,
    headers: Sequence[GitConfigEntry],
    env: dict[str, str],
) -> bool:
    """Ask Git which URL-scoped extra headers apply to one remote."""
    direct_header = env.get("GIT_HTTP_EXTRAHEADER", "")
    if _is_valid_http_extraheader_value(direct_header) and _is_credential_bearing_http_header(
        direct_header
    ):
        return True
    selected = _urlmatched_header_group(remote_url, headers, env)
    active: list[str] = []
    for entry in selected:
        if entry.value.strip():
            active.append(entry.value)
        else:
            active.clear()
    return any(_is_credential_bearing_http_header(value) for value in active)


def _urlmatched_header_group(
    remote_url: str,
    headers: Sequence[GitConfigEntry],
    env: dict[str, str],
) -> tuple[GitConfigEntry, ...]:
    """Return the exact extraHeader key group selected by Git for one URL."""
    if not headers:
        return ()
    probe_env = git_subprocess_env(env)
    probe_env.pop("GIT_CONFIG_PARAMETERS", None)
    probe_env.pop("GIT_CONFIG_COUNT", None)
    for key in tuple(probe_env):
        if key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            probe_env.pop(key, None)
    probe_env["GIT_CONFIG_NOSYSTEM"] = "1"
    probe_env["GIT_CONFIG_SYSTEM"] = os.devnull
    probe_env["GIT_CONFIG_GLOBAL"] = os.devnull
    probe_env["GIT_CONFIG_COUNT"] = str(len(headers))
    for index, entry in enumerate(headers):
        probe_env[f"GIT_CONFIG_KEY_{index}"] = entry.key
        probe_env[f"GIT_CONFIG_VALUE_{index}"] = f"X-Apm-Config-Probe: {index}"

    git_executable = get_git_executable()
    try:
        result = _run_git_config_probe(
            [
                git_executable,
                "config",
                "--null",
                "--get-urlmatch",
                "http.extraHeader",
                remote_url,
            ],
            capture_output=True,
            check=False,
            cwd=str(Path(git_executable).resolve().parent),
            env=probe_env,
            timeout_category="Git URL-match probe timed out",
        )
    except OSError as exc:
        raise GitUrlRewriteProbeError("Git URL-match probe could not start") from exc
    if result.returncode == 1:
        return ()
    if result.returncode != 0 or not isinstance(result.stdout, bytes):
        raise GitUrlRewriteProbeError("Git URL-match probe failed")
    selected = result.stdout.rstrip(b"\0\n")
    prefix = b"X-Apm-Config-Probe: "
    if not selected.startswith(prefix):
        raise GitUrlRewriteProbeError("Git URL-match probe returned malformed output")
    try:
        selected_index = int(selected.removeprefix(prefix))
    except ValueError as exc:
        raise GitUrlRewriteProbeError("Git URL-match probe returned malformed output") from exc
    if selected_index < 0 or selected_index >= len(headers):
        raise GitUrlRewriteProbeError("Git URL-match probe returned malformed output")
    selected_key = headers[selected_index].key.lower()
    return tuple(entry for entry in headers if entry.key.lower() == selected_key)


def git_url_has_authorization(
    remote_url: str,
    headers: Sequence[GitConfigEntry],
    env: dict[str, str] | None = None,
) -> bool:
    """Return whether flattened Git config sends an HTTP header to a URL."""
    return _has_applicable_http_authorization(remote_url, headers, env or {})


def _url_origin(url: str) -> tuple[str, str, int | None]:
    """Return a normalized HTTP(S) origin tuple."""
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    port = parsed.port
    if port is None:
        port = {"http": 80, "https": 443}.get(scheme)
    return scheme, (parsed.hostname or "").lower(), port


def _url_contains_credentials(parsed: SplitResult) -> bool:
    """Return whether URL userinfo can carry a credential."""
    return parsed.password is not None or (
        parsed.scheme.lower() in {"http", "https", "file"} and parsed.username is not None
    )


def _git_url_host(url: str) -> str | None:
    """Return the network host from a URL or Git's SCP-style syntax."""
    parsed = urlsplit(url)
    if parsed.scheme.lower() in {"http", "https", "ssh"}:
        return parsed.hostname.lower() if parsed.hostname else None
    if parsed.scheme:
        return None
    match = _SCP_HOST_RE.match(url)
    if match is None:
        return None
    return match.group(1).strip("[]").lower()


def resolve_git_url_rewrite(
    remote_url: str,
    rewrites: Sequence[tuple[str, str]],
) -> str | None:
    """Apply Git's longest-prefix ``insteadOf`` rule to *remote_url*."""
    matches = tuple(
        (replacement, prefix) for replacement, prefix in rewrites if remote_url.startswith(prefix)
    )
    if not matches:
        return None
    replacement, prefix = max(matches, key=lambda item: len(item[1]))
    return f"{replacement}{remote_url[len(prefix) :]}"


def configured_git_url_policy(
    env: dict[str, object] | None = None,
) -> tuple[tuple[tuple[str, str], ...], tuple[GitConfigEntry, ...]]:
    """Return repository-neutral rewrites and config authorization state."""
    snapshot = _read_effective_git_config(git_subprocess_env(env))
    return snapshot.rewrites, snapshot.http_headers


def validate_resolved_git_url_rewrite(
    remote_url: str,
    effective_url: str,
    *,
    has_authorization: bool,
    managed_auth_intent: bool = False,
) -> None:
    """Validate one effective URL selected by Git's rewrite rules."""
    if _REMOTE_HELPER_RE.match(effective_url):
        raise GitUrlRewriteError(
            "insecure-transport",
            "Git URL rewrite must not select remote-helper syntax",
        )
    try:
        source = urlsplit(remote_url)
        target = urlsplit(effective_url)
        source_origin = _url_origin(remote_url)
        target_origin = _url_origin(effective_url)
    except ValueError as exc:
        raise ValueError("Unable to verify Git URL rewrite safety") from exc
    if _url_contains_credentials(target):
        raise GitUrlRewriteError(
            "credentials",
            "Git URL rewrite replacement must not contain credentials",
        )
    source_scheme = source.scheme.lower()
    target_scheme = target.scheme.lower()
    if target_scheme == "http" and source_scheme != "http":
        raise GitUrlRewriteError(
            "https-downgrade" if source_scheme == "https" else "insecure-transport",
            (
                "HTTPS Git remote must not rewrite to insecure HTTP"
                if source_scheme == "https"
                else "Git remote must not rewrite to insecure HTTP"
            ),
        )
    if target_scheme not in {
        "",
        "file",
        "https",
        "ssh",
    }:
        raise GitUrlRewriteError(
            "insecure-transport",
            "HTTPS Git remote must not rewrite to an insecure transport",
        )
    if _url_contains_credentials(source):
        raise GitUrlRewriteError(
            "credential-origin",
            "Credential-bearing Git remote must not be rewritten",
        )
    if (
        managed_auth_intent
        and target_scheme in {"http", "https"}
        and (source_scheme not in {"http", "https"} or source_origin != target_origin)
    ):
        target_label = (
            f"{target_scheme.upper()} origin"
            if target_scheme in {"http", "https"}
            else "transport origin"
        )
        raise GitUrlRewriteError(
            "credential-origin",
            f"Managed-auth Git remote must not rewrite to a different {target_label}",
        )
    if (
        not managed_auth_intent
        and target_scheme in {"http", "https"}
        and has_authorization
        and source_origin != target_origin
    ):
        raise GitUrlRewriteError(
            "credential-origin",
            f"Authenticated Git remote must not rewrite to a different "
            f"{target_scheme.upper()} origin",
        )
    source_host = _git_url_host(remote_url)
    target_host = _git_url_host(effective_url)
    target_is_network = target_scheme in {"http", "https", "ssh"} or (
        not target_scheme and _SCP_HOST_RE.match(effective_url) is not None
    )
    if source_host and target_is_network and source_host != target_host:
        raise GitUrlRewriteError(
            "cross-host",
            "Git remote must not rewrite to a different network host",
        )


def _validated_git_url_rewrite_policy(
    remote_url: str,
    env: dict[str, str],
    *,
    git_dir: Path | None = None,
    worktree: Path | None = None,
) -> tuple[str | None, _GitConfigSnapshot]:
    """Return one validated effective URL and the exact rewrite snapshot."""
    try:
        snapshot = _read_effective_git_config(
            env,
            git_dir=git_dir,
            worktree=worktree,
        )
    except GitUrlRewriteProbeError:
        raise
    except ValueError as exc:
        raise GitUrlRewriteProbeError("Git config could not be interpreted") from exc

    effective_url = resolve_git_url_rewrite(remote_url, snapshot.rewrites)
    if effective_url is None:
        return None, snapshot
    validate_resolved_git_url_rewrite(
        remote_url,
        effective_url,
        has_authorization=_has_applicable_http_authorization(
            effective_url,
            snapshot.http_headers,
            env,
        ),
        managed_auth_intent=env.get(_MANAGED_GIT_AUTH_INTENT_ENV) == "1",
    )
    return effective_url, snapshot


def validate_git_url_rewrite_safety(
    remote_url: str,
    env: dict[str, str],
    *,
    git_dir: Path | None = None,
    worktree: Path | None = None,
) -> str | None:
    """Reject credential-bearing or HTTPS-downgrading effective URL rewrites."""
    effective_url, _snapshot = _validated_git_url_rewrite_policy(
        remote_url,
        env,
        git_dir=git_dir,
        worktree=worktree,
    )
    return effective_url


def _append_git_url_rewrites(
    env: dict[str, str],
    rewrites: Sequence[tuple[str, str]],
) -> None:
    """Materialize URL rewrites in indexed process configuration."""
    try:
        target_count = max(0, int(env.get("GIT_CONFIG_COUNT", "0") or "0"))
    except GitUrlRewriteProbeError:
        raise
    except ValueError as exc:
        raise GitUrlRewriteProbeError("Git config could not be interpreted") from exc

    existing = {
        (env.get(f"GIT_CONFIG_KEY_{index}", ""), env.get(f"GIT_CONFIG_VALUE_{index}", ""))
        for index in range(target_count)
    }
    for replacement, value in rewrites:
        key = f"url.{replacement}.insteadOf"
        if (key, value) in existing:
            continue
        env[f"GIT_CONFIG_KEY_{target_count}"] = key
        env[f"GIT_CONFIG_VALUE_{target_count}"] = value
        target_count += 1
        existing.add((key, value))
    if target_count:
        env["GIT_CONFIG_COUNT"] = str(target_count)


def set_git_authorization_header(
    env: dict[str, str],
    scheme: str,
    credential: str,
) -> None:
    """Replace Git auth channels with one process-scoped Authorization header."""
    if "\r" in scheme or "\n" in scheme or "\r" in credential or "\n" in credential:
        raise ValueError("scheme and credential must not contain CR or LF")
    clear_git_auth_env(env, remove_helpers=True)
    _append_git_config_entry(env, "credential.helper", "")
    _append_git_config_entry(env, "http.extraheader", f"Authorization: {scheme} {credential}")
    env[_MANAGED_GIT_AUTH_INTENT_ENV] = "1"


def _append_git_config_entry(env: dict[str, str], key: str, value: str) -> None:
    """Append one process-scoped Git config entry."""
    try:
        count = max(0, int(env.get("GIT_CONFIG_COUNT", "0") or "0"))
    except ValueError:
        count = 0
    env["GIT_CONFIG_COUNT"] = str(count + 1)
    env[f"GIT_CONFIG_KEY_{count}"] = key
    env[f"GIT_CONFIG_VALUE_{count}"] = value


def _append_parent_git_config(
    env: dict[str, str],
    *,
    git_dir: Path | None = None,
    worktree: Path | None = None,
) -> _GitConfigSnapshot:
    """Retain parent URL rewrites without restoring config auth channels."""
    try:
        parent_snapshot = _read_effective_git_config(
            git_subprocess_env(),
            git_dir=git_dir,
            worktree=worktree,
        )
    except GitUrlRewriteProbeError:
        raise
    except ValueError as exc:
        raise GitUrlRewriteProbeError("Git config could not be interpreted") from exc
    _append_git_url_rewrites(env, parent_snapshot.rewrites)
    return parent_snapshot


def _merge_parent_git_config_snapshot(
    parent: _GitConfigSnapshot,
    child: _GitConfigSnapshot,
) -> _GitConfigSnapshot:
    """Add flattened parent config while preserving child command precedence."""
    child_entries = {(entry.scope, entry.key, entry.value) for entry in child.entries}
    parent_entries = tuple(
        entry
        for entry in parent.entries
        if not (entry.key.lower().startswith("url.") and entry.key.lower().endswith(".insteadof"))
        and (entry.scope, entry.key, entry.value) not in child_entries
    )
    entries = (*parent_entries, *child.entries)
    return _GitConfigSnapshot(
        entries=entries,
        rewrites=child.rewrites,
        http_headers=tuple(entry for entry in entries if _is_http_extraheader_key(entry.key)),
    )


def _materialize_git_config_snapshot(
    env: dict[str, str],
    snapshot: _GitConfigSnapshot,
    *,
    retain_auth: bool,
    auth_fence: _GitAuthFence | None,
) -> None:
    """Freeze non-local Git config into process entries for execution."""
    env.pop("GIT_CONFIG_PARAMETERS", None)
    env.pop("GIT_CONFIG_COUNT", None)
    for key in tuple(env):
        if key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            env.pop(key, None)

    header_key = (
        f"http.{_http_config_scope(auth_fence.remote_url)}.extraheader"
        if auth_fence is not None
        else None
    )
    retained: list[tuple[str, str]] = []
    for entry in snapshot.entries:
        normalized = entry.key.lower()
        if entry.scope in {"local", "worktree"} and not _is_scope_sensitive_network_config(entry):
            continue
        if normalized == "include.path" or (
            normalized.startswith("includeif.") and normalized.endswith(".path")
        ):
            continue
        if _is_http_extraheader_key(normalized) and not _is_valid_http_extraheader_value(
            entry.value
        ):
            continue
        if not retain_auth and _is_http_extraheader_key(normalized):
            continue
        if auth_fence is not None:
            if header_key is not None and normalized == header_key.lower():
                continue
            if _is_http_extraheader_key(normalized) and (
                not entry.value.strip() or _is_credential_bearing_http_header(entry.value)
            ):
                continue
            if auth_fence.suppress_helpers and _is_credential_helper_key(normalized):
                continue
        retained.append((entry.key, entry.value))

    if auth_fence is not None:
        if auth_fence.suppress_helpers:
            retained.append(("credential.helper", ""))
        if header_key is None:
            raise GitUrlRewriteProbeError("Git auth fence lost its HTTP(S) scope")
        if auth_fence.reset_headers:
            retained.append((header_key, ""))
        retained.extend((header_key, value) for value in auth_fence.safe_headers)
        if auth_fence.managed_header is not None:
            retained.append((header_key, auth_fence.managed_header))

    retained = list(dict.fromkeys(retained))
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    if retained:
        env["GIT_CONFIG_COUNT"] = str(len(retained))
        for index, (key, value) in enumerate(retained):
            env[f"GIT_CONFIG_KEY_{index}"] = key
            env[f"GIT_CONFIG_VALUE_{index}"] = value


def _http_config_scope(remote_url: str) -> str:
    """Return a credential-free URL scope accepted by Git's http config."""
    parsed = urlsplit(remote_url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise GitUrlRewriteProbeError("Git auth fence requires an HTTP(S) URL")
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    netloc = host
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", "", ""))


def _active_header_values(entries: Sequence[GitConfigEntry]) -> tuple[str, ...]:
    """Apply Git's empty-value reset semantics to one selected header group."""
    active: list[str] = []
    for entry in entries:
        if not _is_valid_http_extraheader_value(entry.value):
            continue
        if entry.value.strip():
            active.append(entry.value)
        else:
            active.clear()
    return tuple(active)


def _build_git_auth_fence(
    transport_url: str,
    snapshot: _GitConfigSnapshot,
    env: dict[str, str],
    *,
    intent_snapshot: _GitConfigSnapshot | None = None,
    managed_auth_intent: bool = False,
) -> _GitAuthFence | None:
    """Build the anonymous/native/managed header snapshot for one HTTP URL."""
    if urlsplit(transport_url).scheme.lower() not in {"http", "https"}:
        return None
    intent = intent_snapshot or snapshot
    command_headers = tuple(entry for entry in intent.http_headers if entry.scope == "command")
    command_group = _urlmatched_header_group(transport_url, command_headers, env)
    managed = (
        tuple(
            entry.value
            for entry in command_headers
            if _is_http_extraheader_key(entry.key)
            and _is_credential_bearing_http_header(entry.value)
        )
        if managed_auth_intent
        else ()
    )
    if managed_auth_intent and not managed:
        raise GitUrlRewriteProbeError("managed Git auth intent lost its Authorization header")
    reset_headers = any(not entry.value.strip() for entry in command_group)
    helper_reset = any(
        entry.scope == "command"
        and _is_credential_helper_key(entry.key)
        and not entry.value.strip()
        for entry in intent.entries
    )
    if not managed and not reset_headers and not helper_reset:
        return None

    selected_group = _urlmatched_header_group(transport_url, snapshot.http_headers, env)
    safe_headers = tuple(
        value
        for value in _active_header_values(selected_group)
        if not _is_credential_bearing_http_header(value)
    )
    return _GitAuthFence(
        remote_url=transport_url,
        reset_headers=True,
        suppress_helpers=helper_reset or bool(managed),
        safe_headers=safe_headers,
        managed_header=managed[-1] if managed else None,
    )


def _is_scope_sensitive_network_config(entry: GitConfigEntry) -> bool:
    """Return whether one local entry can affect the validated network child."""
    normalized = entry.key.lower()
    return normalized.startswith("http.") or (
        normalized.startswith("url.") and normalized.endswith(".insteadof")
    )


def _has_scope_sensitive_network_config(snapshot: _GitConfigSnapshot) -> bool:
    """Return whether materialization must be revalidated with repository config."""
    return any(
        entry.scope in {"local", "worktree"} and _is_scope_sensitive_network_config(entry)
        for entry in snapshot.entries
    )


def git_network_env(
    remote_url: str,
    overrides: dict[str, object] | None = None,
    *,
    git_dir: Path | None = None,
    worktree: Path | None = None,
) -> dict[str, str]:
    """Return the canonical validated environment for one network Git URL."""
    env = git_subprocess_env(overrides)
    if not _is_valid_http_extraheader_value(env.get("GIT_HTTP_EXTRAHEADER", "")):
        env.pop("GIT_HTTP_EXTRAHEADER", None)
    managed_auth_intent = env.get(_MANAGED_GIT_AUTH_INTENT_ENV) == "1"
    parent_snapshot: _GitConfigSnapshot | None = None
    if overrides is not None:
        parent_snapshot = _append_parent_git_config(env, git_dir=git_dir, worktree=worktree)
    effective_url, snapshot = _validated_git_url_rewrite_policy(
        remote_url,
        env,
        git_dir=git_dir,
        worktree=worktree,
    )
    transport_url = effective_url or remote_url
    if urlsplit(transport_url).scheme.lower() not in {"http", "https"}:
        clear_git_auth_env(env)
        clear_git_platform_token_env(env, remove=True)
        retain_auth = False
    else:
        retain_auth = True
    intent_snapshot = snapshot
    if parent_snapshot is not None:
        snapshot = _merge_parent_git_config_snapshot(parent_snapshot, snapshot)
    auth_fence = (
        _build_git_auth_fence(
            transport_url,
            snapshot,
            env,
            intent_snapshot=intent_snapshot,
            managed_auth_intent=managed_auth_intent,
        )
        if retain_auth
        else None
    )
    _materialize_git_config_snapshot(
        env,
        snapshot,
        retain_auth=retain_auth,
        auth_fence=auth_fence,
    )
    if _has_scope_sensitive_network_config(snapshot):
        materialized_url, _materialized_snapshot = _validated_git_url_rewrite_policy(
            remote_url,
            env,
            git_dir=git_dir,
            worktree=worktree,
        )
        if materialized_url != effective_url:
            raise GitUrlRewriteProbeError(
                "materialized Git config changed the effective URL rewrite"
            )
    return env


def git_clone_env(
    remote_url: str,
    overrides: dict[str, object] | None,
    target: Path,
    *,
    bare: bool = False,
) -> dict[str, str]:
    """Validate clone config in the Git directory the target will activate."""
    target_existed = target.exists()
    git_dir = target if bare else target / ".git"
    probe_created = not (git_dir / "config").exists()
    target_mode = target.stat().st_mode if target_existed else None
    probe_materialized = False
    try:
        if probe_created:
            if target_existed and any(target.iterdir()):
                raise ValueError(f"Git clone target is not empty: {target}")
            probe_materialized = True
            result = _git_init_run(
                [
                    get_git_executable(),
                    "init",
                    *(("--bare",) if bare else ()),
                    *git_no_templates_args(),
                    "--quiet",
                    str(target),
                ],
                capture_output=True,
                check=False,
                env=git_subprocess_env(overrides),
                timeout=30,
            )
            if result.returncode != 0:
                raise ValueError("Unable to prepare Git clone configuration probe")
            remote_args = [get_git_executable()]
            if bare:
                remote_args.extend(("--git-dir", str(target)))
            else:
                remote_args.extend(("-C", str(target)))
            remote_args.extend(("remote", "add", "origin", remote_url))
            result = _git_init_run(
                remote_args,
                capture_output=True,
                check=False,
                env=git_subprocess_env(overrides),
                timeout=30,
            )
            if result.returncode != 0:
                raise ValueError("Unable to prepare Git clone configuration probe")
        return git_network_env(remote_url, overrides, git_dir=git_dir)
    finally:
        if probe_materialized:
            if bare:
                if target.exists():
                    shutil.rmtree(target)
                if target_existed:
                    target.mkdir()
                    if target_mode is not None:
                        os.chmod(target, target_mode)
            else:
                if git_dir.exists():
                    shutil.rmtree(git_dir)
                if not target_existed and target.exists():
                    target.rmdir()


def git_remote_refs(
    remote_url: str,
    *patterns: str,
    env: dict[str, object] | None = None,
    timeout: int = 30,
    check: bool = False,
    options: Sequence[str] = (),
    git_args: Sequence[str] = (),
) -> subprocess.CompletedProcess[str]:
    """Run ``git ls-remote`` with the canonical validated child environment."""
    child_env = git_network_env(remote_url, env)
    # auth-delegated: callers resolve credential and bearer policy before this executor.
    git_executable = get_git_executable()
    command = [git_executable, *git_args, "ls-remote", *options, remote_url, *patterns]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            cwd=str(Path(git_executable).resolve().parent),
            text=True,
            timeout=timeout,
            env=child_env,
            stdin=subprocess.DEVNULL,
            check=check,
        )
        if isinstance(result.stderr, str):
            result.stderr = redact_git_diagnostic(result.stderr)
        return result
    except subprocess.CalledProcessError as exc:
        if isinstance(exc.stderr, str):
            exc.stderr = redact_git_diagnostic(exc.stderr)
        if isinstance(exc.stdout, str):
            exc.stdout = redact_git_diagnostic(exc.stdout)
        raise
    except subprocess.TimeoutExpired:
        # auth-delegated: callers resolve credential and bearer policy before this executor.
        raise subprocess.TimeoutExpired([git_executable, "ls-remote"], timeout) from None


def init_git_remote_worktree(
    worktree: Path,
    remote_url: str,
    env: dict[str, object],
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict[str, str]:
    """Initialize a worktree and add one validated network remote."""
    git_executable = get_git_executable()
    child_env = git_subprocess_env(env)
    result = run(
        [git_executable, "init", *git_no_templates_args()],
        cwd=str(worktree),
        env=child_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=True,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            result.args,
            output=result.stdout,
            stderr=result.stderr,
        )
    child_env = git_network_env(remote_url, child_env, worktree=worktree)
    result = run(
        [git_executable, "remote", "add", "origin", remote_url],
        cwd=str(worktree),
        env=child_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=True,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            result.args,
            output=result.stdout,
            stderr=result.stderr,
        )
    return git_network_env(remote_url, child_env, worktree=worktree)


def clone_git_worktree(
    url: str,
    target: Path,
    *,
    env: dict[str, object] | None = None,
    depth: int | None = None,
    branch: str | None = None,
    no_checkout: bool = False,
    extra_options: Sequence[str] = (),
    progress: _GitProgress | None = None,
) -> None:
    """Clone a working tree with a complete sanitized child environment."""
    args = [
        get_git_executable(),
        *git_no_hooks_args(),
        "clone",
        *git_no_templates_args(),
    ]
    if progress is not None:
        args.append("--progress")
    if depth is not None:
        args.extend(("--depth", str(depth)))
    if branch is not None:
        args.extend(("--branch", branch))
    if no_checkout:
        args.append("--no-checkout")
    args.extend(extra_options)
    args.extend(("--", url, str(target)))
    clone_env = git_clone_env(url, env, target)
    if progress is None:
        try:
            subprocess.run(
                args,
                check=True,
                capture_output=True,
                text=True,
                timeout=300,
                env=clone_env,
            )
        except subprocess.TimeoutExpired:
            raise subprocess.TimeoutExpired([get_git_executable(), "clone"], 300) from None
        return

    from git.cmd import handle_process_output

    process = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=clone_env,
    )
    stdout: list[str] = []
    stderr: list[str] = []
    progress_handler = progress.new_message_handler()

    def capture_stderr(line: str) -> None:
        stderr.append(line)
        progress_handler(line)

    try:
        handle_process_output(
            process,
            stdout.append,
            capture_stderr,
            decode_streams=False,
            kill_after_timeout=300,
        )
        process.wait(timeout=30)
    except (RuntimeError, subprocess.TimeoutExpired):
        process.kill()
        process.wait()
        raise subprocess.TimeoutExpired([get_git_executable(), "clone"], 300) from None
    if process.returncode != 0:
        raise subprocess.CalledProcessError(
            process.returncode,
            args,
            output="".join(stdout),
            stderr="".join(stderr),
        )


def checkout_git_worktree(
    worktree: Path,
    ref: str,
    *,
    env: dict[str, object] | None = None,
) -> None:
    """Check out a ref in an explicitly located worktree."""
    subprocess.run(
        [
            get_git_executable(),
            *git_no_hooks_args(),
            "-C",
            str(worktree),
            "checkout",
            ref,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
        env=git_subprocess_env(env),
    )


def git_worktree_head(
    worktree: Path,
    *,
    env: dict[str, object] | None = None,
) -> str:
    """Return the HEAD commit for an explicitly located worktree."""
    return git_resolve_commit(worktree, "HEAD", env=env)


def git_resolve_commit(
    worktree: Path,
    ref: str,
    *,
    env: dict[str, object] | None = None,
) -> str:
    """Resolve a ref to a commit in an explicitly located worktree."""
    result = subprocess.run(
        [
            get_git_executable(),
            "-C",
            str(worktree),
            "rev-parse",
            "--verify",
            f"{ref}^{{commit}}",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env=git_subprocess_env(env),
    )
    return result.stdout.strip()


def git_current_branch(
    worktree: Path,
    *,
    env: dict[str, object] | None = None,
) -> str:
    """Return the current branch name for an explicitly located worktree."""
    result = subprocess.run(
        [get_git_executable(), "-C", str(worktree), "symbolic-ref", "--short", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env=git_subprocess_env(env),
    )
    return result.stdout.strip()


def reset_git_cache() -> None:
    """Reset the cached git executable (for testing purposes only)."""
    global _gh_executable, _git_executable
    _git_executable = None
    _gh_executable = None


def git_long_paths_args() -> list[str]:
    """Return ``-c core.longpaths=true`` on Windows, ``[]`` elsewhere.

    Windows enforces a 260-character ``MAX_PATH`` limit by default,
    which the GitCache's deeply-nested ``checkouts_v1/<shard>/<sha>/
    <variant>.incomplete.<pid>.<ns>/`` layout can exceed during
    ``git clone`` -- git fails with ``Filename too long`` while
    creating ``.git/hooks/`` files. Setting ``core.longpaths=true``
    via ``-c`` opts that single subprocess into the long-path API
    without mutating the user's global gitconfig. The flag is a
    no-op on POSIX so callers can prepend it unconditionally.
    """
    if os.name == "nt":
        return ["-c", "core.longpaths=true"]
    return []
