"""Install diagnostics for a repository the host did not serve (#4199, #4174).

One owner for the wording and the runnable next step of "not accessible" and
"not found" failures. Credential sources come from
:meth:`AuthResolver.describe_credential_tried`, which names sources and never
their values.
"""

from __future__ import annotations

from apm_cli.core.auth_outcome import reports_repository_not_found
from apm_cli.models.dependency.host_virtual import dependency_repository_owner
from apm_cli.utils.github_host import default_host


def inaccessible_package_reason(dep_ref, package: str, auth_resolver, verbose: bool) -> str:
    """Explain a package the host did not serve: host, credential tried, next step.

    Names credential sources, never their values. Outside verbose mode the
    reason ends with the one command that shows each probe and credential.
    """
    host = dep_ref.host or default_host()
    tried = (
        auth_resolver.describe_credential_tried(
            host,
            dependency_repository_owner(dep_ref),
            port=dep_ref.port,
            host_type=dep_ref.host_type,
        )
        if auth_resolver is not None
        else "no credential sent"
    )
    reason = f"not accessible or doesn't exist (host: {host}, {tried})"
    if verbose:
        return reason
    return f"{reason}. Run 'apm install --verbose {package}' to see each probe and credential."


def not_found_next_step(error: BaseException, dep_ref) -> str:
    """Return the next step for a direct dependency whose repository is missing."""
    if not reports_repository_not_found(error):
        return ""
    return f" Run 'apm uninstall {dep_ref.get_identity()}' to remove it from apm.yml."
