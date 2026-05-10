"""GitLab direct-shorthand resolution for install package validation."""

from apm_cli.core.auth import AuthResolver
from apm_cli.install.validation import _validate_package_exists
from apm_cli.models.apm_package import DependencyReference

_GITLAB_DIRECT_SHORTHAND_UNRESOLVED = (
    "Direct GitLab host/path did not resolve to a reachable repository with an "
    "installable package path. Use an explicit 'git' URL with a 'path' field "
    "for a deeper project or subdirectory."
)


def _try_resolve_gitlab_direct_shorthand(
    package: str,
    auth_resolver,
    verbose: bool = False,
):
    """Resolve GitLab host/path shorthand to the first reachable repo boundary."""
    if auth_resolver is None:
        auth_resolver = AuthResolver()

    parts = DependencyReference.split_gitlab_direct_shorthand_parts(package)
    if not parts:
        return None
    host, segments, ref = parts
    for (
        repo_url,
        virtual_suffix,
    ) in DependencyReference.iter_gitlab_direct_shorthand_boundary_candidates(segments):
        candidate = DependencyReference.from_gitlab_shorthand_probe(
            host, repo_url, virtual_suffix, ref
        )
        if _validate_package_exists(
            package,
            verbose=verbose,
            auth_resolver=auth_resolver,
            dep_ref=candidate,
        ):
            return candidate
    return None
