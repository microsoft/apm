"""Install diagnostics for a repository the host did not serve (#4199, #4174).

One owner for the wording and the runnable next step of "not accessible" and
"not found" failures. Credential sources come from
:meth:`AuthResolver.describe_credential_tried`, which names sources and never
their values.
"""

from __future__ import annotations

import shlex

from apm_cli.core.auth_outcome import reports_repository_not_found
from apm_cli.deps.transport_selection import initial_transport_scheme as _initial_scheme
from apm_cli.install.validation import _uses_clone_root_probe
from apm_cli.models.dependency.host_virtual import dependency_repository_owner
from apm_cli.utils.github_host import default_host


def inaccessible_package_reason(
    dep_ref, package: str, auth_resolver, verbose: bool, *, user_scope: bool = False
) -> str:
    """Explain a package the host did not serve: host, credential tried, next step.

    Names credential sources, never their values. Outside verbose mode the
    reason ends with the one command that shows each probe and credential,
    quoted for the shell and in the original scope.
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
    scope = "-g " if user_scope else ""
    retry = f"apm install {scope}--verbose {shlex.quote(package)}"
    return f"{reason}. Run '{retry}' to see each probe and credential."


def probe_failure_reason(dep_ref, package: str, auth_resolver, verbose: bool, *, scope=None) -> str:
    """Explain why the install probe could not reach ``package``, keeping its scope."""
    from apm_cli.core.scope import is_user_scope

    clone_root_probe = _uses_clone_root_probe(dep_ref)
    # A virtual subdirectory with an explicit ref exhausts the four-step probe
    # chain; name it instead of the generic "not accessible". A clone-root
    # ``git ls-remote`` probe never ran that chain.
    if (
        dep_ref.is_virtual
        and dep_ref.is_virtual_subdirectory()
        and bool(dep_ref.reference)
        and not clone_root_probe
    ):
        reason = (
            "all probes failed (marker-file, Contents API, git ls-remote, "
            "shallow-fetch) -- verify the path and ref exist and that your "
            "credentials have read access"
        )
        return reason if verbose else reason + " (run with --verbose for the full probe log)"
    if clone_root_probe and _initial_scheme(dep_ref) == "ssh":
        reason = "git ls-remote over SSH failed -- verify that your SSH key has read access to the repository"
        return reason if verbose else reason + " (run with --verbose for the git output)"
    return inaccessible_package_reason(
        dep_ref, package, auth_resolver, verbose, user_scope=is_user_scope(scope)
    )


def not_found_next_step(error: BaseException, dep_ref, *, is_direct: bool, scope=None) -> str:
    """Return the next step for a direct dependency whose repository is missing, in its scope."""
    from apm_cli.core.scope import is_user_scope

    if not is_direct or not reports_repository_not_found(error):
        return ""
    from apm_cli.core.remediation import REPOSITORY_NOT_FOUND

    step = REPOSITORY_NOT_FOUND.bind(package=shlex.quote(dep_ref.get_identity()))
    if is_user_scope(scope):
        step = step.with_command(step.command.replace("apm uninstall ", "apm uninstall -g ", 1))
    return f" {step.sentence()}"
