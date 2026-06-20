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


def build_clone_failure_message(
    *,
    repo_url_base: str,
    plan: Any,
    dep_ref: DependencyReference | None,
    auth_resolver: Any,
    default_host_fn: Callable[[], str],
    last_error: Exception | None,
    sanitize_git_error: Callable[[str], str],
    clone_ctx: CloneFailureContext,
) -> str:
    """Build the aggregate ``RuntimeError`` message for a failed transport plan.

    Extracted from :meth:`GitHubPackageDownloader._execute_transport_plan`
    to keep that module under the file-length guardrail. Pure formatting:
    no I/O, no clone attempts.

    ``clone_ctx`` bundles the six failure-classifier flags (``is_ado``,
    ``is_generic``, ``has_ado_token``, ``has_token``, ``dep_host``,
    ``configured_github_host``); callers construct it before the call.
    """
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

    if last_error:
        sanitized_error = sanitize_git_error(str(last_error))
        error_msg += f" Last error: {sanitized_error}"

    return error_msg
