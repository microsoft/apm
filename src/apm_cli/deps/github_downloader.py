"""GitHub package downloader for APM dependencies."""

import os
import shutil
import stat
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, Callable, List
import random
import re
from typing import Union
import requests

import git
from git import Repo, RemoteProgress
from git.exc import GitCommandError, InvalidGitRepositoryError

from ..core.auth import AuthContext, AuthResolver
from ..models.apm_package import (
    DependencyReference,
    PackageInfo,
    RemoteRef,
    ResolvedReference,
    GitReferenceType,
    PackageType,
    validate_apm_package,
    APMPackage
)
from ..utils.console import _rich_warning
from ..utils.github_host import (
    build_https_clone_url,
    build_ssh_url,
    build_ado_https_clone_url,
    build_ado_ssh_url,
    build_ado_api_url,
    build_raw_content_url,
    build_artifactory_archive_url,
    sanitize_token_url_in_message,
    default_host,
    is_azure_devops_hostname,
    is_github_hostname
)
from ..utils.yaml_io import yaml_to_str
from .transport_selection import (
    GitConfigInsteadOfResolver,
    InsteadOfResolver,
    ProtocolPreference,
    TransportAttempt,
    TransportPlan,
    TransportSelector,
    is_fallback_allowed,
    protocol_pref_from_env,
)
from .git_remote_ops import (
    parse_ls_remote_output,
    semver_sort_key,
    sort_remote_refs,
)
from .download_strategies import DownloadDelegate

# Public docs anchor for the cross-protocol fallback caveat surfaced by the
# #786 warning. Lives under the dependencies guide, next to the canonical
# `--allow-protocol-fallback` section (Starlight site defined in
# docs/astro.config.mjs).
_PROTOCOL_FALLBACK_DOCS_URL = (
    "https://microsoft.github.io/apm/guides/dependencies/"
    "#restoring-the-legacy-permissive-chain"
)


def normalize_collection_path(virtual_path: str) -> str:
    """Normalize a collection virtual path by stripping any existing extension.

    This allows users to specify collection dependencies with or without the extension:
      - owner/repo/collections/name (without extension)
      - owner/repo/collections/name.collection.yml (with extension)

    Args:
        virtual_path: The virtual path from the dependency reference

    Returns:
        str: The normalized path without .collection.yml/.collection.yaml suffix
    """
    for ext in ('.collection.yml', '.collection.yaml'):
        if virtual_path.endswith(ext):
            return virtual_path[:-len(ext)]
    return virtual_path


def _debug(message: str) -> None:
    """Print debug message if APM_DEBUG environment variable is set."""
    if os.environ.get('APM_DEBUG'):
        print(f"[DEBUG] {message}", file=sys.stderr)


def _close_repo(repo) -> None:
    """Release GitPython handles so directories can be deleted on Windows."""
    if repo is None:
        return
    try:
        repo.git.clear_cache()
    except Exception:
        pass
    try:
        repo.close()
    except Exception:
        pass


def _rmtree(path) -> None:
    """Remove a directory tree, handling read-only files and brief Windows locks.

    Delegates to :func:`robust_rmtree` which retries with exponential backoff
    on transient lock errors (e.g. antivirus scanning on Windows).
    """
    from ..utils.file_ops import robust_rmtree
    robust_rmtree(path, ignore_errors=True)


class GitProgressReporter(RemoteProgress):
    """Report git clone progress to Rich Progress."""

    def __init__(self, progress_task_id=None, progress_obj=None, package_name=None):
        super().__init__()
        self.task_id = progress_task_id
        self.progress = progress_obj
        self.package_name = package_name  # Keep consistent name throughout download
        self.last_op = None
        self.disabled = False  # Flag to stop updates after download completes

    def update(self, op_code, cur_count, max_count=None, message=''):
        """Called by GitPython during clone operations."""
        if not self.progress or self.task_id is None or self.disabled:
            return

        # Keep the package name consistent - don't change description to git operations
        # This keeps the UI clean and scannable

        # Update progress bar naturally - let it reach 100%
        if max_count and max_count > 0:
            # Determinate progress (we have total count)
            self.progress.update(
                self.task_id,
                completed=cur_count,
                total=max_count
                # Note: We don't update description - keep the original package name
            )
        else:
            # Indeterminate progress (just show activity)
            self.progress.update(
                self.task_id,
                total=100,  # Set fake total for indeterminate tasks
                completed=min(cur_count, 100) if cur_count else 0
                # Note: We don't update description - keep the original package name
            )

        self.last_op = cur_count

    def _get_op_name(self, op_code):
        """Convert git operation code to human-readable name."""
        from git import RemoteProgress

        # Extract operation type from op_code
        if op_code & RemoteProgress.COUNTING:
            return "Counting objects"
        elif op_code & RemoteProgress.COMPRESSING:
            return "Compressing objects"
        elif op_code & RemoteProgress.WRITING:
            return "Writing objects"
        elif op_code & RemoteProgress.RECEIVING:
            return "Receiving objects"
        elif op_code & RemoteProgress.RESOLVING:
            return "Resolving deltas"
        elif op_code & RemoteProgress.FINDING_SOURCES:
            return "Finding sources"
        elif op_code & RemoteProgress.CHECKING_OUT:
            return "Checking out files"
        else:
            return "Cloning"


class GitHubPackageDownloader:
    """Downloads and validates APM packages from GitHub repositories."""

    def __init__(
        self,
        auth_resolver=None,
        transport_selector: Optional[TransportSelector] = None,
        protocol_pref: Optional[ProtocolPreference] = None,
        allow_fallback: Optional[bool] = None,
    ):
        """Initialize the GitHub package downloader.

        Args:
            auth_resolver: Auth resolver instance. Defaults to a new AuthResolver.
            transport_selector: TransportSelector for protocol decisions.
                Defaults to a new selector with GitConfigInsteadOfResolver.
            protocol_pref: User-stated transport preference for shorthand
                deps. When None, reads APM_GIT_PROTOCOL env.
            allow_fallback: When True, permits cross-protocol fallback
                (legacy behavior). When None, reads
                APM_ALLOW_PROTOCOL_FALLBACK env.
        """
        from apm_cli.core.auth import AuthResolver
        self.auth_resolver = auth_resolver or AuthResolver()
        self.token_manager = self.auth_resolver._token_manager  # Backward compat
        self.git_env = self._setup_git_environment()
        self._transport_selector = transport_selector or TransportSelector()
        self._protocol_pref = (
            protocol_pref if protocol_pref is not None else protocol_pref_from_env()
        )
        self._allow_fallback = (
            allow_fallback if allow_fallback is not None else is_fallback_allowed()
        )
        # Dedup set for the issue #786 cross-protocol port warning: one install
        # run calls _clone_with_fallback multiple times per dep (ref-resolution
        # clone, then the actual dep clone). We want the warning exactly once
        # per (host, repo, port) identity across all those calls.
        self._fallback_port_warned: set = set()

        # Delegate backend-specific download logic to the download delegate.
        self._strategies = DownloadDelegate(host=self)

    def _setup_git_environment(self) -> Dict[str, Any]:
        """Set up Git environment with authentication using centralized token manager.

        Returns:
            Dict containing environment variables for Git operations
        """
        env = self.token_manager.setup_environment()

        # Configure Git security settings
        env['GIT_TERMINAL_PROMPT'] = '0'
        env['GIT_ASKPASS'] = 'echo'  # Prevent interactive credential prompts
        env['GIT_CONFIG_NOSYSTEM'] = '1'

        # Ensure SSH connections fail fast instead of hanging indefinitely when
        # a firewall silently drops packets (common on corporate/VPN networks).
        # If the user already set GIT_SSH_COMMAND we merge our option in;
        # otherwise we create a minimal command with ConnectTimeout.
        _ssh_timeout = '-o ConnectTimeout=30'
        existing_ssh_cmd = os.environ.get('GIT_SSH_COMMAND', '').strip()
        if existing_ssh_cmd:
            if 'connecttimeout' not in existing_ssh_cmd.lower():
                env['GIT_SSH_COMMAND'] = f'{existing_ssh_cmd} {_ssh_timeout}'
            else:
                env['GIT_SSH_COMMAND'] = existing_ssh_cmd
        else:
            env['GIT_SSH_COMMAND'] = f'ssh {_ssh_timeout}'
        if sys.platform == 'win32':
            # 'NUL' fails on some Windows git versions; use an empty temp file.
            import tempfile
            from ..config import get_apm_temp_dir
            temp_base = get_apm_temp_dir() or tempfile.gettempdir()
            empty_cfg = os.path.join(temp_base, '.apm_empty_gitconfig')
            with open(empty_cfg, 'w') as f:
                pass
            env['GIT_CONFIG_GLOBAL'] = empty_cfg
        else:
            env['GIT_CONFIG_GLOBAL'] = '/dev/null'

        # IMPORTANT: Do not resolve credentials via helpers at construction time.
        # AuthResolver.resolve(...) can trigger OS credential helper UI. If we do
        # this eagerly (host-only key) and later resolve per-dependency (host+org),
        # users can see duplicate auth prompts. Keep constructor token state env-only
        # and resolve lazily per dependency during clone/validate flows.
        self.github_token = self.token_manager.get_token_for_purpose('modules', env)
        self.has_github_token = self.github_token is not None
        self._github_token_from_credential_fill = False

        # Azure DevOps (env-only at init; lazy auth resolution happens per dep)
        self.ado_token = self.token_manager.get_token_for_purpose('ado_modules', env)
        self.has_ado_token = self.ado_token is not None

        # JFrog Artifactory (not host-based, uses dedicated env var)
        self.artifactory_token = self.token_manager.get_token_for_purpose('artifactory_modules', env)
        self.has_artifactory_token = self.artifactory_token is not None

        _debug(f"Token setup: has_github_token={self.has_github_token}, has_ado_token={self.has_ado_token}, has_artifactory_token={self.has_artifactory_token}"
               f"{', source=credential_helper' if self._github_token_from_credential_fill else ''}")

        return env

    # --- Registry proxy support ---

    @property
    def registry_config(self):
        """Lazily-constructed :class:`~apm_cli.deps.registry_proxy.RegistryConfig`.

        Returns ``None`` when no registry proxy is configured.
        """
        if not hasattr(self, "_registry_config_cache"):
            from .registry_proxy import RegistryConfig
            self._registry_config_cache = RegistryConfig.from_env()
        return self._registry_config_cache

    # --- Artifactory VCS archive download support ---

    def _get_artifactory_headers(self) -> Dict[str, str]:
        """Backward-compat stub -- delegates to download strategies."""
        return self._strategies.get_artifactory_headers()

    def _download_artifactory_archive(self, host: str, prefix: str, owner: str, repo: str,
                                        ref: str, target_path: Path, scheme: str = "https") -> None:
        """Backward-compat stub -- delegates to download strategies."""
        return self._strategies.download_artifactory_archive(
            host, prefix, owner, repo, ref, target_path, scheme=scheme,
        )

    def _download_file_from_artifactory(self, host: str, prefix: str, owner: str,
                                         repo: str, file_path: str, ref: str, scheme: str = "https") -> bytes:
        """Backward-compat stub -- delegates to download strategies."""
        return self._strategies.download_file_from_artifactory(
            host, prefix, owner, repo, file_path, ref, scheme=scheme,
        )

    @staticmethod
    def _is_artifactory_only() -> bool:
        """Return True when registry-only mode is active.

        Checks the canonical ``PROXY_REGISTRY_ONLY`` env var, falling back to the
        deprecated ``ARTIFACTORY_ONLY`` alias.
        """
        from .registry_proxy import is_enforce_only
        return is_enforce_only()

    def _should_use_artifactory_proxy(self, dep_ref: 'DependencyReference') -> bool:
        """Check if a dependency should be routed through the Artifactory transparent proxy."""
        if dep_ref.is_artifactory():
            return False  # already explicit Artifactory
        if self._is_artifactory_only():
            return True
        if dep_ref.is_azure_devops():
            return False
        host = dep_ref.host or default_host()
        return is_github_hostname(host)

    def _parse_artifactory_base_url(self) -> Optional[tuple]:
        """Return ``(host, prefix, scheme)`` from the registry proxy config, or ``None``.

        Delegates to :meth:`~apm_cli.deps.registry_proxy.RegistryConfig.from_env`
        so that env-var precedence and deprecation warnings are handled in one place.
        """
        from .registry_proxy import RegistryConfig
        cfg = RegistryConfig.from_env()
        if cfg is None:
            return None
        return (cfg.host, cfg.prefix, cfg.scheme)

    def _resolve_dep_token(self, dep_ref: Optional[DependencyReference] = None) -> Optional[str]:
        """Resolve the per-dependency auth token via AuthResolver.

        GitHub and ADO hosts use the token resolved by AuthResolver.
        Generic hosts (GitLab, Bitbucket, etc.) return None so git
        credential helpers can provide credentials instead.

        Args:
            dep_ref: Optional dependency reference for host/org lookup.

        Returns:
            Token string or None.
        """
        if dep_ref is None:
            return self.github_token

        is_ado = dep_ref.is_azure_devops()
        dep_host = dep_ref.host
        if dep_host:
            is_github = is_github_hostname(dep_host)
        else:
            is_github = True
        is_generic = not is_ado and not is_github

        if is_generic:
            return None

        dep_ctx = self.auth_resolver.resolve_for_dep(dep_ref)
        return dep_ctx.token

    def _resolve_dep_auth_ctx(self, dep_ref: Optional[DependencyReference] = None) -> Optional[AuthContext]:
        """Resolve the full AuthContext for a dependency.

        Returns the AuthContext from AuthResolver, or None for generic hosts
        or when no dep_ref is provided.
        """
        if dep_ref is None:
            return None

        is_ado = dep_ref.is_azure_devops()
        dep_host = dep_ref.host
        if dep_host:
            is_github = is_github_hostname(dep_host)
        else:
            is_github = True
        is_generic = not is_ado and not is_github

        if is_generic:
            return None

        ctx = self.auth_resolver.resolve_for_dep(dep_ref)
        # Verbose source surfacing (#852): one-time per-host log line so users
        # can see which credential source was actually used. Routed through
        # AuthResolver.notify_auth_source() (#856 follow-up F2) so the line
        # obeys the same verbose-channel logic as every other diagnostic.
        if os.environ.get("APM_VERBOSE") == "1":
            self.auth_resolver.notify_auth_source(dep_host or "", ctx)
        return ctx

    def _build_noninteractive_git_env(
        self,
        *,
        preserve_config_isolation: bool = False,
        suppress_credential_helpers: bool = False,
    ) -> Dict[str, str]:
        """Return a non-interactive git env for unauthenticated git operations.

        Credential-helper policy (intentional two-stage design):

        1. Start by clearing ``GIT_ASKPASS`` unconditionally. The default
           APM env sets ``GIT_ASKPASS=echo`` for all authenticated ops; for
           unauthenticated fallback attempts (HTTPS/SSH without a token), we
           want the user's system credential helpers (e.g. macOS Keychain,
           Windows credential manager, SSH agent) to resolve naturally.
        2. Then re-set the full credential-helper *suppression* fence ONLY
           when ``suppress_credential_helpers=True`` (HTTP transport). This
           blocks all four credential channels: ``GIT_ASKPASS``,
           ``GIT_TERMINAL_PROMPT``, ``GIT_CONFIG_NOSYSTEM``, and
           ``credential.helper=`` (via ``GIT_CONFIG_COUNT/KEY/VALUE``).

        Do NOT invert or flatten this pop-then-conditionally-restore pattern
        without re-auditing every caller: removing step 1 would leak
        credentials through user helpers on HTTPS/SSH fallbacks; removing
        step 2 would leak them over plaintext HTTP.
        """
        env = dict(self.git_env)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env.pop("GIT_ASKPASS", None)

        if preserve_config_isolation or suppress_credential_helpers:
            env["GIT_CONFIG_NOSYSTEM"] = "1"
            if "GIT_CONFIG_GLOBAL" in self.git_env:
                env["GIT_CONFIG_GLOBAL"] = self.git_env["GIT_CONFIG_GLOBAL"]
        else:
            env.pop("GIT_CONFIG_GLOBAL", None)
            env.pop("GIT_CONFIG_NOSYSTEM", None)

        if suppress_credential_helpers:
            env["GIT_ASKPASS"] = "echo"
            env["GIT_CONFIG_COUNT"] = "1"
            env["GIT_CONFIG_KEY_0"] = "credential.helper"
            env["GIT_CONFIG_VALUE_0"] = ""
        else:
            env.pop("GIT_CONFIG_COUNT", None)
            env.pop("GIT_CONFIG_KEY_0", None)
            env.pop("GIT_CONFIG_VALUE_0", None)

        return env

    def _resilient_get(self, url: str, headers: Dict[str, str], timeout: int = 30, max_retries: int = 3) -> requests.Response:
        """Backward-compat stub -- delegates to download strategies."""
        return self._strategies.resilient_get(url, headers, timeout=timeout, max_retries=max_retries)

    def _sanitize_git_error(self, error_message: str) -> str:
        """Sanitize Git error messages to remove potentially sensitive authentication information.

        Args:
            error_message: Raw error message from Git operations

        Returns:
            str: Sanitized error message with sensitive data removed
        """
        import re

        # Remove any tokens that might appear in URLs for github hosts (format: https://token@host)
        # Sanitize for default host and common enterprise hosts via helper
        sanitized = sanitize_token_url_in_message(error_message, host=default_host())

        # Sanitize Azure DevOps URLs - both cloud (dev.azure.com) and any on-prem server
        # Use a generic pattern to catch https://token@anyhost format for all hosts
        # This catches: dev.azure.com, ado.company.com, tfs.internal.corp, etc.
        sanitized = re.sub(r'https://[^@\s]+@([^\s/]+)', r'https://***@\1', sanitized)

        # Remove any tokens that might appear as standalone values
        sanitized = re.sub(r'(ghp_|gho_|ghu_|ghs_|ghr_)[a-zA-Z0-9_]+', '***', sanitized)

        # Remove environment variable values that might contain tokens
        sanitized = re.sub(r'(GITHUB_TOKEN|GITHUB_APM_PAT|ADO_APM_PAT|GH_TOKEN|GITHUB_COPILOT_PAT)=[^\s]+', r'\1=***', sanitized)

        return sanitized

    def _build_repo_url(self, repo_ref: str, use_ssh: bool = False, dep_ref: DependencyReference = None, token: Optional[str] = None, auth_scheme: str = "basic") -> str:
        """Backward-compat stub -- delegates to download strategies."""
        return self._strategies.build_repo_url(
            repo_ref, use_ssh=use_ssh, dep_ref=dep_ref,
            token=token, auth_scheme=auth_scheme,
        )

    def _clone_with_fallback(self, repo_url_base: str, target_path: Path, progress_reporter=None, dep_ref: DependencyReference = None, verbose_callback=None, **clone_kwargs) -> Repo:
        """Clone a repository following the TransportSelector plan.

        The transport selector decides protocol order and strictness based on
        the user's URL form, CLI/env preferences, and git ``insteadOf`` config.
        Strict-by-default: explicit ``ssh://``, ``https://``, and ``http://``
        URLs no longer silently fall back to a different protocol. To restore
        the legacy permissive chain, set ``--allow-protocol-fallback`` or
        ``APM_ALLOW_PROTOCOL_FALLBACK=1``.

        Args:
            repo_url_base: Base repository reference (owner/repo)
            target_path: Target path for cloning
            progress_reporter: GitProgressReporter instance for progress updates
            dep_ref: DependencyReference for platform/protocol decisions
            verbose_callback: Optional callable for verbose logging (receives str messages)
            **clone_kwargs: Additional arguments for Repo.clone_from

        Returns:
            Repo: Successfully cloned repository

        Raises:
            RuntimeError: If the planned attempt(s) all fail.
        """
        last_error = None
        is_ado = dep_ref and dep_ref.is_azure_devops()

        dep_host = dep_ref.host if dep_ref else None
        if dep_host:
            is_github = is_github_hostname(dep_host)
        else:
            is_github = True
        is_generic = not is_ado and not is_github

        dep_token = self._resolve_dep_token(dep_ref)
        has_token = dep_token is not None

        # Resolve full auth context for bearer-aware URL building and env selection.
        dep_auth_ctx = self._resolve_dep_auth_ctx(dep_ref)
        dep_auth_scheme = dep_auth_ctx.auth_scheme if dep_auth_ctx else "basic"

        _debug(
            f"_clone_with_fallback: repo={repo_url_base}, is_ado={is_ado}, "
            f"is_generic={is_generic}, has_token={has_token}, "
            f"auth_scheme={dep_auth_scheme}, "
            f"protocol_pref={self._protocol_pref.value}, allow_fallback={self._allow_fallback}"
        )

        # Choose the clone env PER ATTEMPT (not per dependency): only the
        # token-bearing attempt should run with the locked-down env that
        # silences credential helpers. SSH and plain-HTTPS attempts in a
        # mixed allow_fallback plan need the relaxed env so user-configured
        # credential helpers (gh auth, Keychain, ssh-agent passphrase
        # prompts) keep working.
        def _env_for(attempt: TransportAttempt) -> Dict[str, str]:
            if attempt.use_token:
                # For ADO bearer auth, use the AuthContext git_env which contains
                # GIT_CONFIG_COUNT/KEY/VALUE for Authorization header injection.
                if dep_auth_scheme == "bearer" and dep_auth_ctx is not None:
                    return dep_auth_ctx.git_env
                return self.git_env
            if attempt.scheme == "http":
                return self._build_noninteractive_git_env(
                    preserve_config_isolation=True,
                    suppress_credential_helpers=True,
                )
            return self._build_noninteractive_git_env()

        plan: TransportPlan = self._transport_selector.select(
            dep_ref=dep_ref,
            cli_pref=self._protocol_pref,
            allow_fallback=self._allow_fallback,
            has_token=has_token,
        )
        _debug(
            "transport plan: "
            f"strict={plan.strict}, attempts={[(a.scheme, a.use_token, a.label) for a in plan.attempts]}"
        )

        # Cross-protocol fallback reuses the dependency's port for every
        # attempt. On servers that serve SSH and HTTPS on different ports
        # (e.g. Bitbucket Datacenter: SSH 7999, HTTPS 7990), the off-protocol
        # URL will be wrong. Warn once per dep, before the first attempt, so
        # the user can pin the URL scheme (and leave fallback disabled) or
        # fail fast by dropping --allow-protocol-fallback. See #786.
        # A single install may call this method multiple times for the same
        # dep (ref resolution + actual clone), so dedup on (host, repo, port).
        dep_port = getattr(dep_ref, "port", None) if dep_ref else None
        if (
            not plan.strict
            and dep_port is not None
            and any(a.scheme == "ssh" for a in plan.attempts)
            and any(a.scheme == "https" for a in plan.attempts)
        ):
            warn_key = (
                dep_host.lower() if dep_host else dep_host,
                repo_url_base,
                dep_port,
            )
            if warn_key not in self._fallback_port_warned:
                self._fallback_port_warned.add(warn_key)
                initial_scheme = plan.attempts[0].scheme.upper()
                fallback_scheme = next(
                    a.scheme.upper()
                    for a in plan.attempts
                    if a.scheme != plan.attempts[0].scheme
                )
                host_display = dep_host or "host"
                _rich_warning(
                    f"Custom port {dep_port} on {host_display}/{repo_url_base}: "
                    f"if {initial_scheme} fails, APM will retry over "
                    f"{fallback_scheme} on the same port.\n"
                    f"    Pin the URL scheme, or drop "
                    f"--allow-protocol-fallback to fail fast.\n"
                    f"    See: {_PROTOCOL_FALLBACK_DOCS_URL}",
                    symbol="warning",
                )

        prev_label: Optional[str] = None
        prev_scheme: Optional[str] = None
        for attempt in plan.attempts:
            # Defensive: skip token-bearing attempts when no token available.
            if attempt.use_token and not has_token:
                continue

            use_ssh = attempt.scheme == "ssh"
            try:
                url = self._build_repo_url(
                    repo_url_base,
                    use_ssh=use_ssh,
                    dep_ref=dep_ref,
                    token=dep_token if attempt.use_token else "",
                    auth_scheme=dep_auth_scheme if attempt.use_token else "basic",
                )
            except Exception as e:
                last_error = e
                continue

            # Surface a [!] warning when the plan permits fallback and we
            # are actually switching git protocols (ssh <-> https) mid-clone
            # rather than just retrying with different auth on the same protocol.
            if (
                not plan.strict
                and prev_label
                and prev_scheme
                and prev_scheme != attempt.scheme
            ):
                _rich_warning(
                    f"Protocol fallback: {prev_label} clone of {repo_url_base} failed; retrying with {attempt.label}.",
                    symbol="warning",
                )

            try:
                _debug(f"Attempting clone with {attempt.label} (URL sanitized)")
                repo = Repo.clone_from(
                    url, target_path, env=_env_for(attempt),
                    progress=progress_reporter, **clone_kwargs,
                )
                if verbose_callback:
                    display = self._sanitize_git_error(url) if attempt.use_token else url
                    verbose_callback(f"Cloned from: {display}")
                return repo
            except GitCommandError as e:
                # ADO bearer fallback for clone (mirrors validation/list_remote_refs):
                # PAT was rejected -> silently retry this attempt with az-cli bearer.
                err_msg = str(e)
                if (
                    is_ado
                    and attempt.use_token
                    and dep_auth_scheme == "basic"
                    and has_token
                    and (
                        "401" in err_msg
                        or "Authentication failed" in err_msg
                        or "Unauthorized" in err_msg
                    )
                ):
                    try:
                        from apm_cli.core.azure_cli import (
                            AzureCliBearerError, get_bearer_provider,
                        )
                        from apm_cli.utils.github_host import build_ado_bearer_git_env
                        provider = get_bearer_provider()
                        if provider.is_available():
                            try:
                                bearer = provider.get_bearer_token()
                                bearer_url = self._build_repo_url(
                                    repo_url_base, use_ssh=False, dep_ref=dep_ref,
                                    token=None, auth_scheme="bearer",
                                )
                                bearer_env = {**self.git_env, **build_ado_bearer_git_env(bearer)}
                                repo = Repo.clone_from(
                                    bearer_url, target_path, env=bearer_env,
                                    progress=progress_reporter, **clone_kwargs,
                                )
                                self.auth_resolver.emit_stale_pat_diagnostic(
                                    dep_host or "dev.azure.com"
                                )
                                if verbose_callback:
                                    verbose_callback(
                                        "Cloned from: (sanitized) via AAD bearer fallback"
                                    )
                                return repo
                            except (AzureCliBearerError, GitCommandError):
                                pass
                    except ImportError:
                        pass
                last_error = e
                prev_label = attempt.label
                prev_scheme = attempt.scheme
                if plan.strict:
                    break

        # All planned attempts failed (or strict-mode single failure)
        if plan.strict and len(plan.attempts) >= 1:
            tried = plan.attempts[0].label
            error_msg = (
                f"Failed to clone repository {repo_url_base} via {tried}. "
            )
            if plan.fallback_hint:
                error_msg += plan.fallback_hint + " "
        else:
            error_msg = f"Failed to clone repository {repo_url_base} using all available methods. "
        configured_host = os.environ.get("GITHUB_HOST", "")
        if is_ado and not self.has_ado_token:
            host = dep_host or "dev.azure.com"
            error_msg += self.auth_resolver.build_error_context(
                host, "clone",
                org=dep_ref.ado_organization if dep_ref else None,
                port=dep_ref.port if dep_ref else None,
                dep_url=dep_ref.repo_url if dep_ref else None,
            )
        elif is_generic:
            if dep_host:
                host_info = self.auth_resolver.classify_host(
                    dep_host, port=dep_ref.port if dep_ref else None,
                )
                host_name = host_info.display_name
            else:
                host_name = "the target host"
            error_msg += (
                f"For private repositories on {host_name}, configure SSH keys or a git credential helper. "
                f"APM delegates authentication to git for non-GitHub/ADO hosts."
            )
        elif configured_host and dep_host and dep_host == configured_host and configured_host != "github.com":
            suggested = f"github.com/{repo_url_base}"
            if dep_ref and dep_ref.virtual_path:
                suggested += f"/{dep_ref.virtual_path}"
            error_msg += (
                f"GITHUB_HOST is set to '{configured_host}', so shorthand dependencies "
                f"(without a hostname) resolve against that host. "
                f"If this package lives on a different server (e.g., github.com), "
                f"use the full hostname in apm.yml: {suggested}"
            )
        elif not has_token:
            # No auth was resolved (neither env var nor credential helper).
            # Guide the user through setting up authentication.
            host = dep_host or default_host()
            org = dep_ref.repo_url.split('/')[0] if dep_ref and dep_ref.repo_url else None
            error_msg += self.auth_resolver.build_error_context(
                host, "clone", org=org, port=dep_ref.port if dep_ref else None,
                dep_url=dep_ref.repo_url if dep_ref else None,
            )
        else:
            error_msg += "Please check repository access permissions and authentication setup."

        if last_error:
            sanitized_error = self._sanitize_git_error(str(last_error))
            error_msg += f" Last error: {sanitized_error}"

        raise RuntimeError(error_msg)

    # ------------------------------------------------------------------
    # Remote ref enumeration (no clone required)
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_ls_remote_output(output: str) -> List[RemoteRef]:
        """Backward-compat stub -- delegates to git_remote_ops."""
        return parse_ls_remote_output(output)

    @staticmethod
    def _semver_sort_key(name: str):
        """Backward-compat stub -- delegates to git_remote_ops."""
        return semver_sort_key(name)

    @classmethod
    def _sort_remote_refs(cls, refs: List[RemoteRef]) -> List[RemoteRef]:
        """Backward-compat stub -- delegates to git_remote_ops."""
        return sort_remote_refs(refs)

    def list_remote_refs(self, dep_ref: DependencyReference) -> List[RemoteRef]:
        """Enumerate remote tags and branches without cloning.

        Uses ``git ls-remote --tags --heads`` for all git hosts (GitHub,
        Azure DevOps, GitLab, generic).  Artifactory dependencies return
        an empty list (no git repo).

        Args:
            dep_ref: Dependency reference describing the remote repo.

        Returns:
            Sorted list of RemoteRef -- tags first (semver descending),
            then branches (alphabetically ascending).

        Raises:
            RuntimeError: If the git command fails.
        """
        # Artifactory: no git repo to query
        if dep_ref.is_artifactory():
            return []

        is_ado = dep_ref.is_azure_devops()
        dep_token = self._resolve_dep_token(dep_ref)
        dep_auth_ctx = self._resolve_dep_auth_ctx(dep_ref)
        dep_auth_scheme = dep_auth_ctx.auth_scheme if dep_auth_ctx else "basic"

        # All git hosts: git ls-remote
        repo_url_base = dep_ref.repo_url

        # Build the env -- mirror _clone_with_fallback logic
        if dep_token:
            # For ADO bearer, use AuthContext git_env with header injection
            if dep_auth_scheme == "bearer" and dep_auth_ctx is not None:
                ls_env = dep_auth_ctx.git_env
            else:
                ls_env = self.git_env
        else:
            ls_env = self._build_noninteractive_git_env(
                preserve_config_isolation=bool(getattr(dep_ref, "is_insecure", False)),
                suppress_credential_helpers=bool(
                    getattr(dep_ref, "is_insecure", False)
                ),
            )

        # Build authenticated URL
        remote_url = self._build_repo_url(
            repo_url_base, use_ssh=False, dep_ref=dep_ref, token=dep_token,
            auth_scheme=dep_auth_scheme,
        )

        try:
            g = git.cmd.Git()
            output = g.ls_remote("--tags", "--heads", remote_url, env=ls_env)
            refs = self._parse_ls_remote_output(output)
            return self._sort_remote_refs(refs)
        except GitCommandError as e:
            # ADO bearer fallback: if PAT was rejected (401/Authentication failed)
            # AND the host is ADO AND we resolved as PAT AND az is available,
            # silently retry with bearer and emit a deferred [!] warning.
            err_str = str(e)
            ado_pat_401 = (
                is_ado
                and dep_auth_scheme == "basic"
                and dep_token is not None
                and ("401" in err_str or "Authentication failed" in err_str or "Unauthorized" in err_str)
            )
            if ado_pat_401:
                try:
                    from apm_cli.core.azure_cli import AzureCliBearerError, get_bearer_provider
                    from apm_cli.utils.github_host import build_ado_bearer_git_env
                    provider = get_bearer_provider()
                    if provider.is_available():
                        try:
                            bearer = provider.get_bearer_token()
                            bearer_env = {**self.git_env, **build_ado_bearer_git_env(bearer)}
                            # Re-build URL WITHOUT token (bearer flows via header)
                            bearer_url = self._build_repo_url(
                                repo_url_base, use_ssh=False, dep_ref=dep_ref,
                                token=None, auth_scheme="bearer",
                            )
                            output = g.ls_remote("--tags", "--heads", bearer_url, env=bearer_env)
                            refs = self._parse_ls_remote_output(output)
                            # Emit stale-PAT diagnostic via the resolver
                            self.auth_resolver.emit_stale_pat_diagnostic(
                                dep_ref.host or default_host()
                            )
                            return self._sort_remote_refs(refs)
                        except (AzureCliBearerError, GitCommandError):
                            pass  # Fall through to original error handling
                except ImportError:
                    pass

            dep_host = dep_ref.host
            if dep_host:
                is_github = is_github_hostname(dep_host)
            else:
                is_github = True
            is_generic = not is_ado and not is_github

            error_msg = f"Failed to list remote refs for {repo_url_base}. "
            if is_generic:
                if dep_host:
                    host_info = self.auth_resolver.classify_host(
                        dep_host, port=dep_ref.port,
                    )
                    host_name = host_info.display_name
                else:
                    host_name = "the target host"
                error_msg += (
                    f"For private repositories on {host_name}, configure SSH keys "
                    f"or a git credential helper. "
                    f"APM delegates authentication to git for non-GitHub/ADO hosts."
                )
            else:
                host = dep_host or default_host()
                org = repo_url_base.split("/")[0] if repo_url_base else None
                error_msg += self.auth_resolver.build_error_context(
                    host, "list refs", org=org,
                    port=dep_ref.port if dep_ref else None,
                    dep_url=dep_ref.repo_url if dep_ref else None,
                )

            sanitized = self._sanitize_git_error(str(e))
            error_msg += f" Last error: {sanitized}"
            raise RuntimeError(error_msg) from e

    def resolve_git_reference(self, repo_ref: Union[str, "DependencyReference"]) -> ResolvedReference:
        """Resolve a Git reference (branch/tag/commit) to a specific commit SHA.

        Args:
            repo_ref: Repository reference — either a DependencyReference object
                or a string (e.g., "user/repo#branch"). Passing the object
                directly avoids a lossy parse round-trip for generic git hosts.

        Returns:
            ResolvedReference: Resolved reference with commit SHA

        Raises:
            ValueError: If the reference format is invalid
            RuntimeError: If Git operations fail
        """
        # Accept both string and DependencyReference to avoid lossy round-trips
        if isinstance(repo_ref, DependencyReference):
            dep_ref = repo_ref
        else:
            try:
                dep_ref = DependencyReference.parse(repo_ref)
            except ValueError as e:
                raise ValueError(f"Invalid repository reference '{repo_ref}': {e}")

        # Use user-specified ref; None means "use the remote's default branch"
        ref = dep_ref.reference or None

        # Normalize to string for ResolvedReference.original_ref
        original_ref_str = str(dep_ref)

        # Artifactory: no git repo to query, return ref-based resolution
        if dep_ref.is_artifactory() or (
            self._parse_artifactory_base_url()
            and self._should_use_artifactory_proxy(dep_ref)
        ):
            effective_ref = ref or "main"
            is_commit = re.match(r'^[a-f0-9]{7,40}$', effective_ref.lower()) is not None
            return ResolvedReference(
                original_ref=original_ref_str,
                ref_type=GitReferenceType.COMMIT if is_commit else GitReferenceType.BRANCH,
                resolved_commit=None,
                ref_name=effective_ref
            )

        # Pre-analyze the reference type to determine the best approach
        is_likely_commit = bool(ref) and re.match(r'^[a-f0-9]{7,40}$', ref.lower()) is not None

        # Create a temporary directory for Git operations
        temp_dir = None
        try:
            from ..config import get_apm_temp_dir
            temp_dir = Path(tempfile.mkdtemp(dir=get_apm_temp_dir()))

            if is_likely_commit:
                # For commit SHAs, clone full repository first, then checkout the commit
                try:
                    # Ensure host is set for enterprise repos
                    repo = self._clone_with_fallback(dep_ref.repo_url, temp_dir, progress_reporter=None, dep_ref=dep_ref)
                    commit = repo.commit(ref)
                    ref_type = GitReferenceType.COMMIT
                    resolved_commit = commit.hexsha
                    ref_name = ref
                except Exception as e:
                    sanitized_error = self._sanitize_git_error(str(e))
                    raise ValueError(f"Could not resolve commit '{ref}' in repository {dep_ref.repo_url}: {sanitized_error}")
            else:
                # For branches and tags, try shallow clone first.
                # When no ref is specified, omit --branch to let git use the remote HEAD.
                try:
                    clone_kwargs = {'depth': 1}
                    if ref:
                        clone_kwargs['branch'] = ref
                    repo = self._clone_with_fallback(
                        dep_ref.repo_url,
                        temp_dir,
                        progress_reporter=None,
                        dep_ref=dep_ref,
                        **clone_kwargs
                    )
                    ref_type = GitReferenceType.BRANCH  # Could be branch or tag
                    resolved_commit = repo.head.commit.hexsha
                    ref_name = ref if ref else repo.active_branch.name

                except GitCommandError:
                    # If branch/tag clone fails, try full clone and resolve reference
                    try:
                        repo = self._clone_with_fallback(dep_ref.repo_url, temp_dir, progress_reporter=None, dep_ref=dep_ref)

                        # Try to resolve the reference
                        try:
                            # Try as branch first
                            try:
                                branch = repo.refs[f"origin/{ref}"]
                                ref_type = GitReferenceType.BRANCH
                                resolved_commit = branch.commit.hexsha
                                ref_name = ref
                            except IndexError:
                                # Try as tag
                                try:
                                    tag = repo.tags[ref]
                                    ref_type = GitReferenceType.TAG
                                    resolved_commit = tag.commit.hexsha
                                    ref_name = ref
                                except IndexError:
                                    raise ValueError(f"Reference '{ref}' not found in repository {dep_ref.repo_url}")

                        except Exception as e:
                            sanitized_error = self._sanitize_git_error(str(e))
                            raise ValueError(f"Could not resolve reference '{ref}' in repository {dep_ref.repo_url}: {sanitized_error}")

                    except GitCommandError as e:
                        # Check if this might be a private repository access issue
                        if "Authentication failed" in str(e) or "remote: Repository not found" in str(e):
                            error_msg = f"Failed to clone repository {dep_ref.repo_url}. "
                            host = dep_ref.host or default_host()
                            org = dep_ref.repo_url.split('/')[0] if dep_ref.repo_url else None
                            error_msg += self.auth_resolver.build_error_context(
                                host, "resolve reference", org=org, port=dep_ref.port,
                                dep_url=dep_ref.repo_url,
                            )
                            raise RuntimeError(error_msg)
                        else:
                            sanitized_error = self._sanitize_git_error(str(e))
                            raise RuntimeError(f"Failed to clone repository {dep_ref.repo_url}: {sanitized_error}")

        finally:
            if temp_dir and temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)

        return ResolvedReference(
            original_ref=original_ref_str,
            ref_type=ref_type,
            resolved_commit=resolved_commit,
            ref_name=ref_name
        )

    def download_raw_file(self, dep_ref: DependencyReference, file_path: str, ref: str = "main", verbose_callback=None) -> bytes:
        """Download a single file from repository (GitHub or Azure DevOps).

        Args:
            dep_ref: Parsed dependency reference
            file_path: Path to file within the repository (e.g., "prompts/code-review.prompt.md")
            ref: Git reference (branch, tag, or commit SHA). Defaults to "main"
            verbose_callback: Optional callable for verbose logging (receives str messages)

        Returns:
            bytes: File content

        Raises:
            RuntimeError: If download fails or file not found
        """
        host = dep_ref.host or default_host()

        # Check if this is Artifactory (Mode 1: explicit FQDN)
        if dep_ref.is_artifactory():
            repo_parts = dep_ref.repo_url.split('/')
            return self._download_file_from_artifactory(
                dep_ref.host, dep_ref.artifactory_prefix,
                repo_parts[0], repo_parts[1] if len(repo_parts) > 1 else repo_parts[0],
                file_path, ref,
            )

        # Check if this should go through Artifactory proxy (Mode 2)
        art_proxy = self._parse_artifactory_base_url()
        if art_proxy and self._should_use_artifactory_proxy(dep_ref):
            repo_parts = dep_ref.repo_url.split('/')
            return self._download_file_from_artifactory(
                art_proxy[0], art_proxy[1],
                repo_parts[0], repo_parts[1] if len(repo_parts) > 1 else repo_parts[0],
                file_path, ref, scheme=art_proxy[2],
            )

        # Check if this is Azure DevOps
        if dep_ref.is_azure_devops():
            return self._download_ado_file(dep_ref, file_path, ref)

        # GitHub API
        return self._download_github_file(dep_ref, file_path, ref, verbose_callback=verbose_callback)

    def _download_ado_file(self, dep_ref: DependencyReference, file_path: str, ref: str = "main") -> bytes:
        """Backward-compat stub -- delegates to download strategies."""
        return self._strategies.download_ado_file(dep_ref, file_path, ref=ref)

    def _try_raw_download(self, owner: str, repo: str, ref: str, file_path: str) -> Optional[bytes]:
        """Backward-compat stub -- delegates to download strategies."""
        return self._strategies.try_raw_download(owner, repo, ref, file_path)

    def _download_github_file(self, dep_ref: DependencyReference, file_path: str, ref: str = "main", verbose_callback=None) -> bytes:
        """Backward-compat stub -- delegates to download strategies."""
        return self._strategies.download_github_file(
            dep_ref, file_path, ref=ref, verbose_callback=verbose_callback,
        )

    def validate_virtual_package_exists(self, dep_ref: DependencyReference) -> bool:
        """Validate that a virtual package (file, collection, or subdirectory) exists on GitHub.

        Supports:
        - Virtual files: owner/repo/path/file.prompt.md
        - Collections: owner/repo/collections/name (checks for .collection.yml)
        - Subdirectory packages: owner/repo/path/subdir (checks for apm.yml, SKILL.md, or plugin.json)

        Args:
            dep_ref: Parsed dependency reference for virtual package

        Returns:
            bool: True if the package exists and is accessible, False otherwise
        """
        if not dep_ref.is_virtual:
            raise ValueError("Can only validate virtual packages with this method")

        ref = dep_ref.reference or "main"
        file_path = dep_ref.virtual_path

        # For collections, check for .collection.yml file
        if dep_ref.is_virtual_collection():
            file_path = f"{dep_ref.virtual_path}.collection.yml"
            try:
                self.download_raw_file(dep_ref, file_path, ref)
                return True
            except RuntimeError:
                return False

        # For virtual files, check the file directly
        if dep_ref.is_virtual_file():
            try:
                self.download_raw_file(dep_ref, file_path, ref)
                return True
            except RuntimeError:
                return False

        # For subdirectory packages: apm.yml or SKILL.md confirm the type;
        # plugin.json confirms a Claude plugin; README.md is a last-resort
        # signal that the directory exists (any directory that follows the
        # Claude plugin spec may have none of the above).
        if dep_ref.is_virtual_subdirectory():
            # Try apm.yml first
            try:
                self.download_raw_file(dep_ref, f"{dep_ref.virtual_path}/apm.yml", ref)
                return True
            except RuntimeError:
                pass

            # Try SKILL.md
            try:
                self.download_raw_file(dep_ref, f"{dep_ref.virtual_path}/SKILL.md", ref)
                return True
            except RuntimeError:
                pass

            # Try plugin.json at various plugin locations
            plugin_locations = [
                f"{dep_ref.virtual_path}/plugin.json",                                    # Root
                f"{dep_ref.virtual_path}/.github/plugin/plugin.json",                     # GitHub Copilot format
                f"{dep_ref.virtual_path}/.claude-plugin/plugin.json",                     # Claude format
                f"{dep_ref.virtual_path}/.cursor-plugin/plugin.json",                     # Cursor format
            ]

            for plugin_path in plugin_locations:
                try:
                    self.download_raw_file(dep_ref, plugin_path, ref)
                    return True
                except RuntimeError:
                    continue

            # Last resort: README.md  -- any well-formed directory should have one.
            # A directory that follows the Claude plugin spec (agents/, commands/,
            # skills/ ...) with no manifest files is still a valid plugin.
            try:
                self.download_raw_file(dep_ref, f"{dep_ref.virtual_path}/README.md", ref)
                return True
            except RuntimeError:
                pass

        # Fallback: try to download the file directly
        try:
            self.download_raw_file(dep_ref, file_path, ref)
            return True
        except RuntimeError:
            return False

    def download_virtual_file_package(self, dep_ref: DependencyReference, target_path: Path, progress_task_id=None, progress_obj=None) -> PackageInfo:
        """Download a single file as a virtual APM package.

        Creates a minimal APM package structure with the file placed in the appropriate
        .apm/ subdirectory based on its extension.

        Args:
            dep_ref: Dependency reference with virtual_path set
            target_path: Local path where virtual package should be created
            progress_task_id: Rich Progress task ID for progress updates
            progress_obj: Rich Progress object for progress updates

        Returns:
            PackageInfo: Information about the created virtual package

        Raises:
            ValueError: If the dependency is not a valid virtual file package
            RuntimeError: If download fails
        """
        if not dep_ref.is_virtual or not dep_ref.virtual_path:
            raise ValueError("Dependency must be a virtual file package")

        if not dep_ref.is_virtual_file():
            raise ValueError(f"Path '{dep_ref.virtual_path}' is not a valid individual file. "
                           f"Must end with one of: {', '.join(DependencyReference.VIRTUAL_FILE_EXTENSIONS)}")

        # Determine the ref to use
        ref = dep_ref.reference or "main"

        # Update progress - downloading
        if progress_obj and progress_task_id is not None:
            progress_obj.update(progress_task_id, completed=50, total=100)

        # Download the file content
        try:
            file_content = self.download_raw_file(dep_ref, dep_ref.virtual_path, ref)
        except RuntimeError as e:
            raise RuntimeError(f"Failed to download virtual package: {e}")

        # Update progress - processing
        if progress_obj and progress_task_id is not None:
            progress_obj.update(progress_task_id, completed=90, total=100)

        # Create target directory structure
        target_path.mkdir(parents=True, exist_ok=True)

        # Determine the subdirectory based on file extension
        subdirs = {
            '.prompt.md': 'prompts',
            '.instructions.md': 'instructions',
            '.chatmode.md': 'chatmodes',
            '.agent.md': 'agents'
        }

        subdir = None
        filename = dep_ref.virtual_path.split('/')[-1]
        for ext, dir_name in subdirs.items():
            if dep_ref.virtual_path.endswith(ext):
                subdir = dir_name
                break

        if not subdir:
            raise ValueError(f"Unknown file extension for {dep_ref.virtual_path}")

        # Create .apm structure
        apm_dir = target_path / ".apm" / subdir
        apm_dir.mkdir(parents=True, exist_ok=True)

        # Write the file
        file_path = apm_dir / filename
        file_path.write_bytes(file_content)

        # Generate minimal apm.yml
        package_name = dep_ref.get_virtual_package_name()

        # Try to extract description from file frontmatter
        description = f"Virtual package containing {filename}"
        try:
            content_str = file_content.decode('utf-8')
            # Simple frontmatter parsing (YAML between --- markers)
            if content_str.startswith('---\n'):
                end_idx = content_str.find('\n---\n', 4)
                if end_idx > 0:
                    frontmatter = content_str[4:end_idx]
                    # Look for description field
                    for line in frontmatter.split('\n'):
                        if line.startswith('description:'):
                            description = line.split(':', 1)[1].strip().strip('"\'')
                            break
        except Exception:
            # If frontmatter parsing fails, use default description
            pass

        apm_yml_data = {
            "name": package_name,
            "version": "1.0.0",
            "description": description,
            "author": dep_ref.repo_url.split('/')[0],
        }
        apm_yml_content = yaml_to_str(apm_yml_data)

        apm_yml_path = target_path / "apm.yml"
        apm_yml_path.write_text(apm_yml_content, encoding='utf-8')

        # Create APMPackage object
        package = APMPackage(
            name=package_name,
            version="1.0.0",
            description=description,
            author=dep_ref.repo_url.split('/')[0],
            source=dep_ref.to_github_url(),
            package_path=target_path
        )

        # Return PackageInfo
        return PackageInfo(
            package=package,
            install_path=target_path,
            installed_at=datetime.now().isoformat(),
            dependency_ref=dep_ref  # Store for canonical dependency string
        )

    def download_collection_package(self, dep_ref: DependencyReference, target_path: Path, progress_task_id=None, progress_obj=None) -> PackageInfo:
        """Download a collection as a virtual APM package.

        Downloads the collection manifest, then fetches all referenced files and
        organizes them into the appropriate .apm/ subdirectories.

        Args:
            dep_ref: Dependency reference with virtual_path pointing to collection
            target_path: Local path where virtual package should be created
            progress_task_id: Rich Progress task ID for progress updates
            progress_obj: Rich Progress object for progress updates

        Returns:
            PackageInfo: Information about the created virtual package

        Raises:
            ValueError: If the dependency is not a valid collection package
            RuntimeError: If download fails
        """
        if not dep_ref.is_virtual or not dep_ref.virtual_path:
            raise ValueError("Dependency must be a virtual collection package")

        if not dep_ref.is_virtual_collection():
            raise ValueError(f"Path '{dep_ref.virtual_path}' is not a valid collection path")

        # Determine the ref to use
        ref = dep_ref.reference or "main"

        # Update progress - starting
        if progress_obj and progress_task_id is not None:
            progress_obj.update(progress_task_id, completed=10, total=100)

        # Normalize virtual_path by stripping .collection.yml/.yaml suffix if already present
        # This allows users to specify either:
        #   - owner/repo/collections/name (without extension)
        #   - owner/repo/collections/name.collection.yml (with extension)
        virtual_path_base = normalize_collection_path(dep_ref.virtual_path)

        # Extract collection name from normalized path (e.g., "collections/project-planning" -> "project-planning")
        collection_name = virtual_path_base.split('/')[-1]

        # Build collection manifest path - try .yml first, then .yaml as fallback
        collection_manifest_path = f"{virtual_path_base}.collection.yml"

        # Download the collection manifest
        try:
            manifest_content = self.download_raw_file(dep_ref, collection_manifest_path, ref)
        except RuntimeError as e:
            # Try .yaml extension as fallback
            if ".collection.yml" in str(e):
                collection_manifest_path = f"{virtual_path_base}.collection.yaml"
                try:
                    manifest_content = self.download_raw_file(dep_ref, collection_manifest_path, ref)
                except RuntimeError:
                    raise RuntimeError(f"Collection manifest not found: {virtual_path_base}.collection.yml (also tried .yaml)")
            else:
                raise RuntimeError(f"Failed to download collection manifest: {e}")

        # Parse the collection manifest
        from .collection_parser import parse_collection_yml

        try:
            manifest = parse_collection_yml(manifest_content)
        except (ValueError, Exception) as e:
            raise RuntimeError(f"Invalid collection manifest '{collection_name}': {e}")

        # Create target directory structure
        target_path.mkdir(parents=True, exist_ok=True)

        # Download all items from the collection
        downloaded_count = 0
        failed_items = []
        total_items = len(manifest.items)

        for idx, item in enumerate(manifest.items):
            # Update progress for each item
            if progress_obj and progress_task_id is not None:
                progress_percent = 20 + int((idx / total_items) * 70)  # 20% to 90%
                progress_obj.update(progress_task_id, completed=progress_percent, total=100)

            try:
                # Download the file
                item_content = self.download_raw_file(dep_ref, item.path, ref)

                # Determine subdirectory based on item kind
                subdir = item.subdirectory

                # Create the subdirectory
                apm_subdir = target_path / ".apm" / subdir
                apm_subdir.mkdir(parents=True, exist_ok=True)

                # Write the file
                filename = item.path.split('/')[-1]
                file_path = apm_subdir / filename
                file_path.write_bytes(item_content)

                downloaded_count += 1

            except RuntimeError as e:
                # Log the failure but continue with other items
                failed_items.append(f"{item.path} ({e})")
                continue

        # Check if we downloaded at least some items
        if downloaded_count == 0:
            error_msg = f"Failed to download any items from collection '{collection_name}'"
            if failed_items:
                error_msg += f". Failures:\n  - " + "\n  - ".join(failed_items)
            raise RuntimeError(error_msg)

        # Generate apm.yml with collection metadata
        package_name = dep_ref.get_virtual_package_name()

        apm_yml_data = {
            "name": package_name,
            "version": "1.0.0",
            "description": manifest.description,
            "author": dep_ref.repo_url.split('/')[0],
        }
        if manifest.tags:
            apm_yml_data["tags"] = list(manifest.tags)
        apm_yml_content = yaml_to_str(apm_yml_data)

        apm_yml_path = target_path / "apm.yml"
        apm_yml_path.write_text(apm_yml_content, encoding='utf-8')

        # Create APMPackage object
        package = APMPackage(
            name=package_name,
            version="1.0.0",
            description=manifest.description,
            author=dep_ref.repo_url.split('/')[0],
            source=dep_ref.to_github_url(),
            package_path=target_path
        )

        # Log warnings for failed items if any
        if failed_items:
            import warnings
            warnings.warn(
                f"Collection '{collection_name}' installed with {downloaded_count}/{manifest.item_count} items. "
                f"Failed items: {len(failed_items)}"
            )

        # Return PackageInfo
        return PackageInfo(
            package=package,
            install_path=target_path,
            installed_at=datetime.now().isoformat(),
            dependency_ref=dep_ref  # Store for canonical dependency string
        )

    def _try_sparse_checkout(self, dep_ref: DependencyReference, temp_clone_path: Path, subdir_path: str, ref: str = None) -> bool:
        """Attempt sparse-checkout to download only a subdirectory (git 2.25+).

        Returns True on success. Falls back silently on failure.
        """
        import subprocess
        try:
            temp_clone_path.mkdir(parents=True, exist_ok=True)

            # Resolve per-dependency token via AuthResolver.
            dep_token = self._resolve_dep_token(dep_ref)
            dep_auth_ctx = self._resolve_dep_auth_ctx(dep_ref)
            dep_auth_scheme = dep_auth_ctx.auth_scheme if dep_auth_ctx else "basic"

            # For ADO bearer, use the AuthContext git_env with header injection
            if dep_auth_scheme == "bearer" and dep_auth_ctx is not None:
                env = {**os.environ, **(dep_auth_ctx.git_env or {})}
            else:
                env = {**os.environ, **(self.git_env or {})}
            auth_url = self._build_repo_url(dep_ref.repo_url, use_ssh=False, dep_ref=dep_ref, token=dep_token, auth_scheme=dep_auth_scheme)

            cmds = [
                ['git', 'init'],
                ['git', 'remote', 'add', 'origin', auth_url],
                ['git', 'sparse-checkout', 'init', '--cone'],
                ['git', 'sparse-checkout', 'set', subdir_path],
            ]
            fetch_cmd = ['git', 'fetch', 'origin']
            fetch_cmd.append(ref or 'HEAD')
            fetch_cmd.append('--depth=1')
            cmds.append(fetch_cmd)
            cmds.append(['git', 'checkout', 'FETCH_HEAD'])

            for cmd in cmds:
                result = subprocess.run(
                    cmd, cwd=str(temp_clone_path), env=env,
                    capture_output=True, text=True, encoding="utf-8", timeout=120,
                )
                if result.returncode != 0:
                    _debug(f"Sparse-checkout step failed ({' '.join(cmd)}): {result.stderr.strip()}")
                    return False

            return True
        except Exception as e:
            _debug(f"Sparse-checkout failed: {e}")
            return False

    def download_subdirectory_package(self, dep_ref: DependencyReference, target_path: Path, progress_task_id=None, progress_obj=None) -> PackageInfo:
        """Download a subdirectory from a repo as an APM package.

        Used for Claude Skills or APM packages nested in monorepos.
        Clones the repo, extracts the subdirectory, and cleans up.

        Args:
            dep_ref: Dependency reference with virtual_path set to subdirectory
            target_path: Local path where package should be created
            progress_task_id: Rich Progress task ID for progress updates
            progress_obj: Rich Progress object for progress updates

        Returns:
            PackageInfo: Information about the downloaded package

        Raises:
            ValueError: If the dependency is not a valid subdirectory package
            RuntimeError: If download or validation fails
        """
        if not dep_ref.is_virtual or not dep_ref.virtual_path:
            raise ValueError("Dependency must be a virtual subdirectory package")

        if not dep_ref.is_virtual_subdirectory():
            raise ValueError(f"Path '{dep_ref.virtual_path}' is not a valid subdirectory package")

        # Use user-specified ref, or None to use repo's default branch
        ref = dep_ref.reference  # None if not specified
        subdir_path = dep_ref.virtual_path

        # Update progress - starting
        if progress_obj and progress_task_id is not None:
            progress_obj.update(progress_task_id, completed=10, total=100)

        # Use mkdtemp + explicit cleanup so we control when rmtree runs.
        # tempfile.TemporaryDirectory().__exit__ calls shutil.rmtree without our
        # retry logic, which raises WinError 32 when git processes still hold
        # handles at the end of the with-block.
        from ..config import get_apm_temp_dir
        temp_dir = None
        try:
            temp_dir = tempfile.mkdtemp(dir=get_apm_temp_dir())
            # Sparse checkout always targets "repo/".  If it fails we clone into
            # "repo_clone/" so we never have to rmtree a directory that may still
            # have live git handles from the failed subprocess.
            sparse_clone_path = Path(temp_dir) / "repo"
            temp_clone_path = sparse_clone_path

            # Update progress - cloning
            if progress_obj and progress_task_id is not None:
                progress_obj.update(progress_task_id, completed=20, total=100)

            # Phase 4 (#171): Try sparse-checkout first (git 2.25+), fall back to full clone
            sparse_ok = self._try_sparse_checkout(dep_ref, sparse_clone_path, subdir_path, ref)

            if not sparse_ok:
                # Full clone into a fresh subdirectory so we don't have to touch
                # the (possibly locked) sparse-checkout directory at all.
                temp_clone_path = Path(temp_dir) / "repo_clone"

                package_display_name = subdir_path.split('/')[-1]
                progress_reporter = GitProgressReporter(progress_task_id, progress_obj, package_display_name) if progress_task_id and progress_obj else None

                # Detect if ref is a commit SHA (can't be used with --branch in shallow clones)
                is_commit_sha = ref and re.match(r'^[a-f0-9]{7,40}$', ref) is not None

                clone_kwargs = {
                    'dep_ref': dep_ref,
                }
                if is_commit_sha:
                    # For commit SHAs, clone without checkout then checkout the specific commit.
                    # Shallow clone doesn't support fetching by arbitrary SHA.
                    clone_kwargs['no_checkout'] = True
                else:
                    clone_kwargs['depth'] = 1
                    if ref:
                        clone_kwargs['branch'] = ref

                try:
                    self._clone_with_fallback(
                        dep_ref.repo_url,
                        temp_clone_path,
                        progress_reporter=progress_reporter,
                        **clone_kwargs
                    )
                except Exception as e:
                    raise RuntimeError(f"Failed to clone repository: {e}") from e

                if is_commit_sha:
                    repo_obj = None
                    try:
                        repo_obj = Repo(temp_clone_path)
                        repo_obj.git.checkout(ref)
                    except Exception as e:
                        raise RuntimeError(f"Failed to checkout commit {ref}: {e}") from e
                    finally:
                        _close_repo(repo_obj)

                # Disable progress reporter after clone
                if progress_reporter:
                    progress_reporter.disabled = True

            # Update progress - extracting subdirectory
            if progress_obj and progress_task_id is not None:
                progress_obj.update(progress_task_id, completed=70, total=100)

            # Check if subdirectory exists
            source_subdir = temp_clone_path / subdir_path
            # Security: ensure subdirectory resolves within the cloned repo
            from ..utils.path_security import ensure_path_within
            ensure_path_within(source_subdir, temp_clone_path)
            if not source_subdir.exists():
                raise RuntimeError(f"Subdirectory '{subdir_path}' not found in repository")

            if not source_subdir.is_dir():
                raise RuntimeError(f"Path '{subdir_path}' is not a directory")

            # Create target directory
            target_path.mkdir(parents=True, exist_ok=True)

            # If target exists and has content, remove it
            if target_path.exists() and any(target_path.iterdir()):
                _rmtree(target_path)
                target_path.mkdir(parents=True, exist_ok=True)

            # Copy subdirectory contents to target (retry on transient
            # file-lock errors caused by antivirus scanning on Windows).
            from ..utils.file_ops import robust_copytree, robust_copy2
            for item in source_subdir.iterdir():
                src = source_subdir / item.name
                dst = target_path / item.name
                if src.is_dir():
                    robust_copytree(src, dst)
                else:
                    robust_copy2(src, dst)

            # Capture commit SHA; close the Repo object immediately so its file
            # handles are released before _rmtree() runs in the finally block.
            repo = None
            try:
                repo = Repo(temp_clone_path)
                resolved_commit = repo.head.commit.hexsha
            except Exception:
                resolved_commit = "unknown"
            finally:
                _close_repo(repo)

            # Update progress - validating
            if progress_obj and progress_task_id is not None:
                progress_obj.update(progress_task_id, completed=90, total=100)

        except PermissionError as exc:
            exc_path = getattr(exc, 'filename', None)
            # If temp_dir wasn't created (mkdtemp failed) or the error is within
            # the temp tree, this is likely a restricted temp directory issue.
            if temp_dir is None or (exc_path and str(exc_path).startswith(str(temp_dir))):
                raise RuntimeError(
                    "Access denied in temporary directory"
                    + (f" '{temp_dir}'" if temp_dir else "")
                    + ". Corporate security may restrict this path. "
                    "Fix: apm config set temp-dir <WRITABLE_PATH>"
                ) from None
            raise
        except OSError as exc:
            if getattr(exc, 'errno', None) == 13 or getattr(exc, 'winerror', None) == 5:
                exc_path = getattr(exc, 'filename', None)
                if temp_dir is None or (exc_path and str(exc_path).startswith(str(temp_dir))):
                    raise RuntimeError(
                        "Access denied in temporary directory"
                        + (f" '{temp_dir}'" if temp_dir else "")
                        + ". Corporate security may restrict this path. "
                        "Fix: apm config set temp-dir <WRITABLE_PATH>"
                    ) from None
            raise
        finally:
            if temp_dir:
                _rmtree(temp_dir)

        # Validate the extracted package (after temp dir is cleaned up)
        validation_result = validate_apm_package(target_path)
        if not validation_result.is_valid:
            error_msgs = "; ".join(validation_result.errors)
            raise RuntimeError(f"Subdirectory is not a valid APM package or Claude Skill: {error_msgs}")

        # Get the resolved reference for metadata
        resolved_ref = ResolvedReference(
            original_ref=ref or "default",
            ref_name=ref or "default",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit=resolved_commit
        )

        # For plugins without an explicit version, stamp with the short commit SHA.
        package = validation_result.package
        if (
            validation_result.package_type == PackageType.MARKETPLACE_PLUGIN
            and package.version == "0.0.0"
            and resolved_commit != "unknown"
        ):
            short_sha = resolved_commit[:7]
            package.version = short_sha
            apm_yml_path = target_path / "apm.yml"
            if apm_yml_path.exists():
                from ..utils.yaml_io import load_yaml, dump_yaml
                _data = load_yaml(apm_yml_path) or {}
                _data["version"] = short_sha
                dump_yaml(_data, apm_yml_path)

        # Update progress - complete
        if progress_obj and progress_task_id is not None:
            progress_obj.update(progress_task_id, completed=100, total=100)

        return PackageInfo(
            package=package,
            install_path=target_path,
            resolved_reference=resolved_ref,
            installed_at=datetime.now().isoformat(),
            dependency_ref=dep_ref,
            package_type=validation_result.package_type
        )

    def _download_subdirectory_from_artifactory(
        self, dep_ref: 'DependencyReference', target_path: Path,
        proxy_info: tuple, progress_task_id=None, progress_obj=None,
    ) -> PackageInfo:
        """Download an archive from Artifactory and extract a subdirectory."""
        import tempfile
        from ..config import get_apm_temp_dir
        ref = dep_ref.reference or "main"
        subdir_path = dep_ref.virtual_path
        repo_parts = dep_ref.repo_url.split('/')
        owner, repo = repo_parts[0], repo_parts[1] if len(repo_parts) > 1 else repo_parts[0]
        host, prefix, scheme = proxy_info

        if progress_obj and progress_task_id is not None:
            progress_obj.update(progress_task_id, completed=10, total=100)

        with tempfile.TemporaryDirectory(dir=get_apm_temp_dir()) as temp_dir:
            temp_path = Path(temp_dir) / "full_pkg"
            self._download_artifactory_archive(host, prefix, owner, repo, ref, temp_path, scheme=scheme)
            if progress_obj and progress_task_id is not None:
                progress_obj.update(progress_task_id, completed=60, total=100)
            source_subdir = temp_path / subdir_path
            if not source_subdir.exists() or not source_subdir.is_dir():
                raise RuntimeError(
                    f"Subdirectory '{subdir_path}' not found in archive from "
                    f"Artifactory ({host}/{prefix}/{owner}/{repo}#{ref})"
                )
            target_path.mkdir(parents=True, exist_ok=True)
            from ..utils.file_ops import robust_rmtree, robust_copytree, robust_copy2
            if target_path.exists() and any(target_path.iterdir()):
                robust_rmtree(target_path)
                target_path.mkdir(parents=True, exist_ok=True)
            for item in source_subdir.iterdir():
                src = source_subdir / item.name
                dst = target_path / item.name
                if src.is_dir():
                    robust_copytree(src, dst)
                else:
                    robust_copy2(src, dst)

        if progress_obj and progress_task_id is not None:
            progress_obj.update(progress_task_id, completed=80, total=100)
        validation_result = validate_apm_package(target_path)
        if not validation_result.is_valid:
            raise RuntimeError(f"Subdirectory is not a valid APM package: {'; '.join(validation_result.errors)}")
        resolved_ref = ResolvedReference(original_ref=ref, ref_name=ref, ref_type=GitReferenceType.BRANCH, resolved_commit=None)
        if progress_obj and progress_task_id is not None:
            progress_obj.update(progress_task_id, completed=100, total=100)
        return PackageInfo(
            package=validation_result.package, install_path=target_path,
            resolved_reference=resolved_ref, installed_at=datetime.now().isoformat(),
            dependency_ref=dep_ref, package_type=validation_result.package_type
        )

    def _download_package_from_artifactory(
        self, dep_ref: 'DependencyReference', target_path: Path,
        proxy_info: Optional[tuple] = None, progress_task_id=None, progress_obj=None,
    ) -> PackageInfo:
        """Download a package via Artifactory VCS archive."""
        ref = dep_ref.reference or "main"
        repo_parts = dep_ref.repo_url.split('/')
        if len(repo_parts) < 2 or not repo_parts[0] or not repo_parts[1]:
            raise ValueError(f"Invalid Artifactory repo reference '{dep_ref.repo_url}': expected 'owner/repo' format")
        owner, repo = repo_parts[0], repo_parts[1]

        scheme = "https"
        if dep_ref.is_artifactory():
            host, prefix = dep_ref.host, dep_ref.artifactory_prefix
            if not host or not prefix:
                raise ValueError(f"Artifactory dependency '{dep_ref.repo_url}' is missing host or artifactory prefix")
        elif proxy_info:
            host, prefix, scheme = proxy_info
        else:
            raise RuntimeError("Artifactory download requires either FQDN or ARTIFACTORY_BASE_URL")

        _debug(f"Downloading from Artifactory: {host}/{prefix}/{owner}/{repo}#{ref}")
        if target_path.exists() and any(target_path.iterdir()):
            from ..utils.file_ops import robust_rmtree
            robust_rmtree(target_path)
        target_path.mkdir(parents=True, exist_ok=True)
        if progress_obj and progress_task_id is not None:
            progress_obj.update(progress_task_id, total=100, completed=10)
        try:
            self._download_artifactory_archive(host, prefix, owner, repo, ref, target_path, scheme=scheme)
        except RuntimeError:
            if target_path.exists():
                shutil.rmtree(target_path, ignore_errors=True)
            raise
        if progress_obj and progress_task_id is not None:
            progress_obj.update(progress_task_id, completed=70, total=100)

        validation_result = validate_apm_package(target_path)
        if not validation_result.is_valid:
            if target_path.exists():
                shutil.rmtree(target_path, ignore_errors=True)
            error_msg = f"Invalid APM package {dep_ref.repo_url}:\n"
            for error in validation_result.errors:
                error_msg += f"  - {error}\n"
            raise RuntimeError(error_msg.strip())
        if not validation_result.package:
            raise RuntimeError(f"Package validation succeeded but no package metadata found for {dep_ref.repo_url}")
        package = validation_result.package
        package.source = dep_ref.to_github_url()
        package.resolved_commit = None
        resolved_ref = ResolvedReference(original_ref=f"{dep_ref.repo_url}#{ref}", ref_type=GitReferenceType.BRANCH, resolved_commit=None, ref_name=ref)
        if progress_obj and progress_task_id is not None:
            progress_obj.update(progress_task_id, completed=100, total=100)
        return PackageInfo(
            package=package, install_path=target_path, resolved_reference=resolved_ref,
            installed_at=datetime.now().isoformat(), dependency_ref=dep_ref,
            package_type=validation_result.package_type
        )

    def download_package(
        self,
        repo_ref: Union[str, "DependencyReference"],
        target_path: Path,
        progress_task_id=None,
        progress_obj=None,
        verbose_callback=None
    ) -> PackageInfo:
        """Download a GitHub repository and validate it as an APM package.

        For virtual packages (individual files or collections), creates a minimal
        package structure instead of cloning the full repository.

        Args:
            repo_ref: Repository reference — either a DependencyReference object
                or a string (e.g., "user/repo#branch"). Passing the object
                directly avoids a lossy parse round-trip for generic git hosts.
            target_path: Local path where package should be downloaded
            progress_task_id: Rich Progress task ID for progress updates
            progress_obj: Rich Progress object for progress updates
            verbose_callback: Optional callable for verbose logging (receives str messages)

        Returns:
            PackageInfo: Information about the downloaded package

        Raises:
            ValueError: If the repository reference is invalid
            RuntimeError: If download or validation fails
        """
        # Accept both string and DependencyReference to avoid lossy round-trips
        if isinstance(repo_ref, DependencyReference):
            dep_ref = repo_ref
        else:
            try:
                dep_ref = DependencyReference.parse(repo_ref)
            except ValueError as e:
                raise ValueError(f"Invalid repository reference '{repo_ref}': {e}")

        # Handle virtual packages differently
        if dep_ref.is_virtual:
            art_proxy = self._parse_artifactory_base_url()
            if self._is_artifactory_only() and not dep_ref.is_artifactory() and not art_proxy:
                raise RuntimeError(
                    f"PROXY_REGISTRY_ONLY is set but no Artifactory proxy is configured for '{repo_ref}'. "
                    "Set PROXY_REGISTRY_URL or use explicit Artifactory FQDN syntax."
                )
            if dep_ref.is_virtual_file():
                return self.download_virtual_file_package(dep_ref, target_path, progress_task_id, progress_obj)
            elif dep_ref.is_virtual_collection():
                return self.download_collection_package(dep_ref, target_path, progress_task_id, progress_obj)
            elif dep_ref.is_virtual_subdirectory():
                # Mode 1: explicit Artifactory FQDN from lockfile
                if dep_ref.is_artifactory():
                    proxy_info = (dep_ref.host, dep_ref.artifactory_prefix, "https")
                    return self._download_subdirectory_from_artifactory(
                        dep_ref, target_path, proxy_info, progress_task_id, progress_obj
                    )
                # Mode 2: transparent proxy via env var (art_proxy computed above)
                if self._is_artifactory_only() and art_proxy:
                    return self._download_subdirectory_from_artifactory(
                        dep_ref, target_path, art_proxy, progress_task_id, progress_obj
                    )
                return self.download_subdirectory_package(dep_ref, target_path, progress_task_id, progress_obj)
            else:
                raise ValueError(f"Unknown virtual package type for {dep_ref.virtual_path}")

        # Artifactory download path (Mode 1: explicit FQDN, Mode 2: transparent proxy)
        use_artifactory = dep_ref.is_artifactory()
        art_proxy = None
        if not use_artifactory:
            art_proxy = self._parse_artifactory_base_url()
            if art_proxy and self._should_use_artifactory_proxy(dep_ref):
                use_artifactory = True

        if use_artifactory:
            return self._download_package_from_artifactory(
                dep_ref, target_path, art_proxy, progress_task_id, progress_obj
            )

        # When PROXY_REGISTRY_ONLY is set but no Artifactory proxy matched, block direct git
        if self._is_artifactory_only():
            raise RuntimeError(
                f"PROXY_REGISTRY_ONLY is set but no Artifactory proxy is configured for '{dep_ref}'. "
                "Set PROXY_REGISTRY_URL or use explicit Artifactory FQDN syntax."
            )

        # Regular package download (existing logic)
        resolved_ref = self.resolve_git_reference(dep_ref)

        # Create target directory if it doesn't exist
        target_path.mkdir(parents=True, exist_ok=True)

        # If directory already exists and has content, remove it
        if target_path.exists() and any(target_path.iterdir()):
            _rmtree(target_path)
            target_path.mkdir(parents=True, exist_ok=True)

        # Store progress reporter so we can disable it after clone
        progress_reporter = None
        package_display_name = dep_ref.repo_url.split('/')[-1] if '/' in dep_ref.repo_url else dep_ref.repo_url

        try:
            # Clone the repository using fallback authentication methods
            # Use shallow clone for performance if we have a specific commit
            if resolved_ref.ref_type == GitReferenceType.COMMIT:
                # For commits, we need to clone and checkout the specific commit
                progress_reporter = GitProgressReporter(progress_task_id, progress_obj, package_display_name) if progress_task_id and progress_obj else None
                repo = self._clone_with_fallback(
                    dep_ref.repo_url,
                    target_path,
                    progress_reporter=progress_reporter,
                    dep_ref=dep_ref,
                    verbose_callback=verbose_callback
                )
                repo.git.checkout(resolved_ref.resolved_commit)
            else:
                # For branches and tags, we can use shallow clone
                progress_reporter = GitProgressReporter(progress_task_id, progress_obj, package_display_name) if progress_task_id and progress_obj else None
                repo = self._clone_with_fallback(
                    dep_ref.repo_url,
                    target_path,
                    progress_reporter=progress_reporter,
                    dep_ref=dep_ref,
                    verbose_callback=verbose_callback,
                    depth=1,
                    branch=resolved_ref.ref_name
                )

            # Disable progress reporter to prevent late git updates
            if progress_reporter:
                progress_reporter.disabled = True

            # Remove .git directory to save space and prevent treating as a Git repository
            git_dir = target_path / ".git"
            if git_dir.exists():
                shutil.rmtree(git_dir, ignore_errors=True)

        except GitCommandError as e:
            # Check if this might be a private repository access issue
            if "Authentication failed" in str(e) or "remote: Repository not found" in str(e):
                error_msg = f"Failed to clone repository {dep_ref.repo_url}. "
                host = dep_ref.host or default_host()
                org = dep_ref.repo_url.split('/')[0] if dep_ref.repo_url else None
                error_msg += self.auth_resolver.build_error_context(
                    host, "clone", org=org, port=dep_ref.port,
                    dep_url=dep_ref.repo_url,
                )
                raise RuntimeError(error_msg)
            else:
                sanitized_error = self._sanitize_git_error(str(e))
                raise RuntimeError(f"Failed to clone repository {dep_ref.repo_url}: {sanitized_error}")
        except RuntimeError:
            # Re-raise RuntimeError from _clone_with_fallback
            raise

        # Validate the downloaded package
        validation_result = validate_apm_package(target_path)
        if not validation_result.is_valid:
            # Clean up on validation failure
            if target_path.exists():
                shutil.rmtree(target_path, ignore_errors=True)

            error_msg = f"Invalid APM package {dep_ref.repo_url}:\n"
            for error in validation_result.errors:
                error_msg += f"  - {error}\n"
            raise RuntimeError(error_msg.strip())

        # Load the APM package metadata
        if not validation_result.package:
            raise RuntimeError(f"Package validation succeeded but no package metadata found for {dep_ref.repo_url}")

        package = validation_result.package
        package.source = dep_ref.to_github_url()
        package.resolved_commit = resolved_ref.resolved_commit

        # For plugins without an explicit version, use the short commit SHA so the
        # lock file and conflict detection have a meaningful, stable version string.
        if (
            validation_result.package_type == PackageType.MARKETPLACE_PLUGIN
            and package.version == "0.0.0"
            and resolved_ref.resolved_commit
        ):
            short_sha = resolved_ref.resolved_commit[:7]
            package.version = short_sha
            # Keep the synthesized apm.yml in sync
            apm_yml_path = target_path / "apm.yml"
            if apm_yml_path.exists():
                from ..utils.yaml_io import load_yaml, dump_yaml
                _data = load_yaml(apm_yml_path) or {}
                _data["version"] = short_sha
                dump_yaml(_data, apm_yml_path)

        # Create and return PackageInfo
        return PackageInfo(
            package=package,
            install_path=target_path,
            resolved_reference=resolved_ref,
            installed_at=datetime.now().isoformat(),
            dependency_ref=dep_ref,  # Store for canonical dependency string
            package_type=validation_result.package_type  # Track if APM, Claude Skill, or Hybrid
        )

    def _get_clone_progress_callback(self):
        """Get a progress callback for Git clone operations.

        Returns:
            Callable that can be used as progress callback for GitPython
        """
        def progress_callback(op_code, cur_count, max_count=None, message=''):
            """Progress callback for Git operations."""
            if max_count:
                percentage = int((cur_count / max_count) * 100)
                print(f"\r Cloning: {percentage}% ({cur_count}/{max_count}) {message}", end='', flush=True)
            else:
                print(f"\r Cloning: {message} ({cur_count})", end='', flush=True)

        return progress_callback
