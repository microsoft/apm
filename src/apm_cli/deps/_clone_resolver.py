"""Branch/tag clone resolver extracted from git_reference_resolver.py."""

from __future__ import annotations

from pathlib import Path

from git.exc import GitCommandError

from ..models.apm_package import DependencyReference, GitReferenceType
from ..utils.github_host import default_host


def _resolve_branch_or_tag(
    host,
    dep_ref: DependencyReference,
    ref: str | None,
    temp_dir: Path,
) -> tuple:
    """Clone the repo (shallow first, full fallback) and resolve *ref*.

    Returns ``(ref_type, resolved_commit, ref_name)``.
    """
    try:
        clone_kwargs: dict = {"depth": 1}
        if ref:
            clone_kwargs["branch"] = ref
        repo = host._clone_with_fallback(
            dep_ref.repo_url,
            temp_dir,
            progress_reporter=None,
            dep_ref=dep_ref,
            **clone_kwargs,
        )
        ref_name = ref if ref else repo.active_branch.name
        return GitReferenceType.BRANCH, repo.head.commit.hexsha, ref_name
    except GitCommandError:
        try:
            repo = host._clone_with_fallback(
                dep_ref.repo_url, temp_dir, progress_reporter=None, dep_ref=dep_ref
            )
            try:
                try:
                    branch = repo.refs[f"origin/{ref}"]
                    return GitReferenceType.BRANCH, branch.commit.hexsha, ref
                except IndexError:
                    try:
                        tag = repo.tags[ref]
                        return GitReferenceType.TAG, tag.commit.hexsha, ref
                    except IndexError:
                        raise ValueError(  # noqa: B904
                            f"Reference '{ref}' not found in repository {dep_ref.repo_url}"
                        )
            except Exception as e:
                sanitized_error = host._sanitize_git_error(str(e))
                raise ValueError(  # noqa: B904
                    f"Could not resolve reference '{ref}' in repository "
                    f"{dep_ref.repo_url}: {sanitized_error}"
                )
        except GitCommandError as e:
            if "Authentication failed" in str(e) or "remote: Repository not found" in str(e):
                error_msg = f"Failed to clone repository {dep_ref.repo_url}. "
                target_host = dep_ref.host or default_host()
                org = dep_ref.repo_url.split("/")[0] if dep_ref.repo_url else None
                error_msg += host.auth_resolver.build_error_context(
                    target_host,
                    "resolve reference",
                    org=org,
                    port=dep_ref.port,
                    dep_url=dep_ref.repo_url,
                )
                raise RuntimeError(error_msg)  # noqa: B904
            else:
                sanitized_error = host._sanitize_git_error(str(e))
                raise RuntimeError(  # noqa: B904
                    f"Failed to clone repository {dep_ref.repo_url}: {sanitized_error}"
                )
