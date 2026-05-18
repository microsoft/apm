"""clone_with_fallback and build_clone_failure_message: working-tree clone helpers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from git import Repo

from ._scrub import _rmtree

if TYPE_CHECKING:
    from ...models.apm_package import DependencyReference


@dataclass(frozen=True, slots=True)
class WtCloneOpts:
    """Keyword-only arguments for :func:`clone_with_fallback`."""

    progress_reporter: Any = None
    dep_ref: DependencyReference | None = None
    verbose_callback: Callable[[str], None] | None = None
    repo_cls: Any = None


@dataclass
class CloneFailureContext:
    """Bundled arguments for :func:`build_clone_failure_message`."""

    repo_url_base: str
    plan: Any
    dep_ref: Any  # DependencyReference | None
    dep_host: str | None
    is_ado: bool
    is_generic: bool
    has_ado_token: bool
    has_token: bool
    auth_resolver: Any
    configured_github_host: str
    default_host_fn: Callable[[], str]
    last_error: Exception | None
    sanitize_git_error: Callable[[str], str]


def clone_with_fallback(
    execute_transport_plan: Callable[..., None],
    repo_url_base: str,
    target_path: Path,
    opts: WtCloneOpts | None = None,
    **clone_kwargs: Any,
) -> Repo:
    """Clone a working-tree repository following the TransportSelector plan.

    Thin adapter over the caller-supplied ``execute_transport_plan``
    callable (typically ``self._execute_transport_plan``) that supplies
    a working-tree clone action (``Repo.clone_from``). Behavior is
    unchanged from the pre-#1126 implementation, except every clone
    attempt now begins with a robust ``_rmtree`` of the target
    for symmetry with the bare-clone path. This is strictly safer
    (clean slate per attempt) and matches the existing behavior on
    the second-and-subsequent attempts where target may contain a
    partial clone from the failed first attempt.

    Returns:
        The successfully cloned :class:`Repo`.

    Raises:
        RuntimeError: If the planned attempt(s) all fail.
    """
    repo_holder: list[Repo] = []
    if opts is None:
        opts = WtCloneOpts()
    progress_reporter = opts.progress_reporter
    dep_ref = opts.dep_ref
    verbose_callback = opts.verbose_callback
    _repo = opts.repo_cls if opts.repo_cls is not None else Repo

    def _wt_action(url: str, env: dict[str, str], target: Path) -> None:
        # Pre-attempt cleanup: GitPython's Repo.clone_from refuses a
        # non-empty target. Symmetric with _bare_action so retries
        # always start from a clean slate. Behavior change from the
        # pre-#1126 implementation - covered by 6.13.
        if target.exists():
            _rmtree(target)
        repo_holder.append(
            _repo.clone_from(
                url,
                target,
                env=env,
                progress=progress_reporter,
                **clone_kwargs,
            )
        )

    execute_transport_plan(
        repo_url_base,
        target_path,
        dep_ref=dep_ref,
        clone_action=_wt_action,
        verbose_callback=verbose_callback,
    )
    return repo_holder[0]


def build_clone_failure_message(ctx: CloneFailureContext) -> str:
    """Build the aggregate ``RuntimeError`` message for a failed transport plan.

    Extracted from :meth:`GitHubPackageDownloader._execute_transport_plan`
    to keep that module under the file-length guardrail. Pure formatting:
    no I/O, no clone attempts.
    """
    repo_url_base = ctx.repo_url_base
    plan = ctx.plan
    dep_ref = ctx.dep_ref
    dep_host = ctx.dep_host
    is_ado = ctx.is_ado
    is_generic = ctx.is_generic
    has_ado_token = ctx.has_ado_token
    has_token = ctx.has_token
    auth_resolver = ctx.auth_resolver
    configured_github_host = ctx.configured_github_host
    default_host_fn = ctx.default_host_fn
    last_error = ctx.last_error
    sanitize_git_error = ctx.sanitize_git_error
    if plan.strict and len(plan.attempts) >= 1:
        tried = plan.attempts[0].label
        error_msg = f"Failed to clone repository {repo_url_base} via {tried}. "
        if plan.fallback_hint:
            error_msg += plan.fallback_hint + " "
    else:
        error_msg = f"Failed to clone repository {repo_url_base} using all available methods. "
    if is_ado and not has_ado_token:
        host = dep_host or "dev.azure.com"
        error_msg += auth_resolver.build_error_context(
            host,
            "clone",
            org=dep_ref.ado_organization if dep_ref else None,
            port=dep_ref.port if dep_ref else None,
            dep_url=dep_ref.repo_url if dep_ref else None,
        )
    elif is_generic:
        if dep_host:
            host_info = auth_resolver.classify_host(
                dep_host,
                port=dep_ref.port if dep_ref else None,
            )
            host_name = host_info.display_name
        else:
            host_name = "the target host"
        error_msg += (
            f"For private repositories on {host_name}, configure SSH keys or a git credential helper. "
            f"APM delegates authentication to git for non-GitHub/ADO hosts."
        )
    elif (
        configured_github_host
        and dep_host
        and dep_host == configured_github_host
        and configured_github_host != "github.com"
    ):
        suggested = f"github.com/{repo_url_base}"
        if dep_ref and dep_ref.virtual_path:
            suggested += f"/{dep_ref.virtual_path}"
        error_msg += (
            f"GITHUB_HOST is set to '{configured_github_host}', so shorthand dependencies "
            f"(without a hostname) resolve against that host. "
            f"If this package lives on a different server (e.g., github.com), "
            f"use the full hostname in apm.yml: {suggested}"
        )
    elif not has_token:
        host = dep_host or default_host_fn()
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
