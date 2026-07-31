"""Clone-failure message builder for the WS2 dedup pipeline.

Extracted from :mod:`bare_cache` to keep that module under the
file-length guardrail.  Re-exported from ``bare_cache`` so callers
see no change.

Public names:
* :class:`CloneFailureContext` -- frozen dataclass bundling the six
  classifier flags / host-info params.
* :func:`build_clone_failure_message` -- aggregate error-message
  formatter.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..models.apm_package import DependencyReference


@dataclass(frozen=True)
class CloneFailureContext:
    """Classifier flags and host info for :func:`build_clone_failure_message`.

    Bundles the six parameters that describe the *kind* of failure so
    the caller constructs the context once and passes a single cohesive
    argument instead of six separate keyword arguments.
    """

    is_ado: bool
    is_generic: bool
    has_ado_token: bool
    has_token: bool
    dep_host: str | None
    configured_github_host: str


def _git_failure_text(error: Exception) -> str:
    """Return captured command text for internal marker classification only.

    The result may contain credentials, key paths, or remote-controlled text.
    Never surface it to users, logs, or exception messages.
    """
    parts = [str(error)]
    for attr in ("stderr", "stdout"):
        stream = getattr(error, attr, None)
        if not stream:
            continue
        if isinstance(stream, bytes):
            parts.append(stream.decode("utf-8", errors="replace"))
        else:
            parts.append(str(stream))
    return " ".join(part.strip() for part in parts if part and part.strip())


def _is_ssh_key_auth_failure(error_text: str, last_attempt_scheme: str | None) -> bool:
    """Return True for passphrase or public-key errors from an SSH attempt."""
    if (last_attempt_scheme or "").lower() != "ssh":
        return False

    text = error_text.lower()
    markers = (
        "enter passphrase for key",
        "incorrect passphrase supplied",
        "bad passphrase",
        "read_passphrase",
        "permission denied (publickey)",
    )
    return any(marker in text for marker in markers)


def _ssh_key_auth_diagnostic(
    last_error: Exception | None,
    last_attempt_scheme: str | None,
) -> str:
    """Build an SSH key diagnostic, or an empty string when unrelated."""
    if last_error is None:
        return ""
    if not _is_ssh_key_auth_failure(_git_failure_text(last_error), last_attempt_scheme):
        return ""
    return (
        " SSH key authentication failed while APM ran git non-interactively. "
        "Verify that the key is available to SSH. For a passphrase-protected key, "
        "unlock it before running APM (for example, with 'ssh-add <key-file>'). "
        "In CI, load a dedicated deploy key non-interactively, or switch the "
        "dependency to token-backed HTTPS. APM does not open an interactive "
        "passphrase prompt during clone."
    )


def _clone_ctx_from_kwargs(
    is_ado: bool,
    is_generic: bool,
    has_ado_token: bool,
    has_token: bool,
    dep_host: str | None,
    configured_github_host: str,
) -> CloneFailureContext:
    """Assemble a :class:`CloneFailureContext` from individual classifier flags."""
    return CloneFailureContext(
        is_ado=is_ado,
        is_generic=is_generic,
        has_ado_token=has_ado_token,
        has_token=has_token,
        dep_host=dep_host,
        configured_github_host=configured_github_host,
    )


def build_clone_failure_message(  # noqa: PLR0913
    *,
    repo_url_base: str,
    plan: Any,
    dep_ref: DependencyReference | None,
    auth_resolver: Any,
    default_host_fn: Callable[[], str],
    last_error: Exception | None,
    last_attempt_scheme: str | None,
    sanitize_git_error: Callable[[str], str],
    public_github_non_auth_failure: bool = False,
    clone_ctx: CloneFailureContext | None = None,
    is_ado: bool = False,
    is_generic: bool = False,
    has_ado_token: bool = False,
    has_token: bool = False,
    dep_host: str | None = None,
    configured_github_host: str = "",
) -> str:
    """Build the aggregate ``RuntimeError`` message for a failed transport plan.

    Extracted from :meth:`GitHubPackageDownloader._execute_transport_plan`
    to keep that module under the file-length guardrail. Pure formatting:
    no I/O, no clone attempts.

    Callers may pass either ``clone_ctx`` (preferred) or the six individual
    classifier flags (backward compat).
    """
    if clone_ctx is None:
        clone_ctx = _clone_ctx_from_kwargs(
            is_ado=is_ado,
            is_generic=is_generic,
            has_ado_token=has_ado_token,
            has_token=has_token,
            dep_host=dep_host,
            configured_github_host=configured_github_host,
        )
    if plan.strict and len(plan.attempts) >= 1:
        tried = plan.attempts[0].label
        error_msg = f"Failed to clone repository {repo_url_base} via {tried}. "
        if plan.fallback_hint:
            error_msg += plan.fallback_hint + " "
    else:
        error_msg = f"Failed to clone repository {repo_url_base} using all available methods. "

    if clone_ctx.is_ado and not clone_ctx.has_ado_token:
        host = clone_ctx.dep_host or "dev.azure.com"
        error_msg += auth_resolver.build_error_context(
            host,
            "clone",
            org=dep_ref.ado_organization if dep_ref else None,
            port=dep_ref.port if dep_ref else None,
            dep_url=dep_ref.repo_url if dep_ref else None,
        )
    elif clone_ctx.is_generic:
        if clone_ctx.dep_host:
            host_info = auth_resolver.classify_host(
                clone_ctx.dep_host,
                port=dep_ref.port if dep_ref else None,
            )
            host_name = host_info.display_name
        else:
            host_name = "the target host"
        error_msg += (
            f"For private repositories on {host_name}, configure SSH keys "
            f"or a git credential helper. "
            f"APM delegates authentication to git for non-GitHub/ADO hosts."
        )
    elif (
        clone_ctx.configured_github_host
        and clone_ctx.dep_host
        and clone_ctx.dep_host == clone_ctx.configured_github_host
        and clone_ctx.configured_github_host != "github.com"
    ):
        suggested = f"github.com/{repo_url_base}"
        if dep_ref and dep_ref.virtual_path:
            suggested += f"/{dep_ref.virtual_path}"
        error_msg += (
            f"GITHUB_HOST is set to '{clone_ctx.configured_github_host}', "
            f"so shorthand dependencies (without a hostname) resolve against that host. "
            f"If this package lives on a different server (e.g., github.com), "
            f"use the full hostname in apm.yml: {suggested}"
        )
    elif public_github_non_auth_failure:
        error_msg += (
            f"Could not connect to {clone_ctx.dep_host or default_host_fn()} "
            "(network error, not an auth failure). "
            "Check your internet connection and proxy settings. "
            "Run with --verbose for details."
        )
    elif not clone_ctx.has_token:
        host = clone_ctx.dep_host or default_host_fn()
        org = dep_ref.repo_url.split("/")[0] if dep_ref and dep_ref.repo_url else None
        error_msg += auth_resolver.build_error_context(
            host,
            "clone",
            org=org,
            port=dep_ref.port if dep_ref else None,
            dep_url=dep_ref.repo_url if dep_ref else None,
        )
    else:
        error_msg += "Please check repository access permissions and authentication setup."

    error_msg += _ssh_key_auth_diagnostic(last_error, last_attempt_scheme)

    if last_error:
        sanitized_error = sanitize_git_error(str(last_error))
        error_msg += f" Last error: {sanitized_error}"

    return error_msg
