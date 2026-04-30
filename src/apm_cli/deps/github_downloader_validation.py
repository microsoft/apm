"""Virtual-package validation helpers for ``GitHubPackageDownloader``.

Extracted from ``github_downloader.py`` to keep the downloader module under
the repo's 2400-line cap.  These helpers were added by PR #941 to align
``apm install`` validation with the actual install auth chain (Contents
API directory probe + ``git ls-remote`` fallback) so subdirectory
packages with an explicit ``#ref`` no longer false-fail when the API
token is narrower than the user's git credential helper.

The helpers are module-level functions taking the downloader instance as
the first argument; the public class still exposes
``validate_virtual_package_exists`` as a thin delegating method so test
mocks (``patch("...GitHubPackageDownloader.validate_virtual_package_exists")``)
keep working unchanged.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING
from urllib.parse import quote

import git
import requests
from git.exc import GitCommandError

from ..utils.github_host import default_host, is_github_hostname

if TYPE_CHECKING:
    from .dependency_reference import DependencyReference
    from .github_downloader import GitHubPackageDownloader


_SHA_RE = re.compile(r"[0-9a-fA-F]{7,40}")


def _is_sha_pin(ref: str) -> bool:
    """Return True when ``ref`` looks like an abbreviated or full git SHA."""
    return bool(_SHA_RE.fullmatch(ref))


def validate_virtual_package_exists(
    downloader: GitHubPackageDownloader,
    dep_ref: DependencyReference,
    verbose_callback: Callable[[str], None] | None = None,
    warn_callback: Callable[[str], None] | None = None,
) -> bool:
    """Validate that a virtual package exists at ``dep_ref``.

    Supports virtual files, collections, and subdirectory packages.  For
    subdirectory packages the marker-file probes are a fast positive
    signal; their absence is not a failure -- two fallbacks (Contents API
    directory probe, then ``git ls-remote`` mirroring the install auth
    chain) catch packages whose API auth is stricter than their git
    auth.  See PR #941 for the auth-alignment rationale.

    Args:
        downloader: The ``GitHubPackageDownloader`` instance providing
            transport, auth, and helper methods.
        dep_ref: Parsed dependency reference for the virtual package.
        verbose_callback: Optional per-probe log callback (verbose mode).
        warn_callback: Optional non-verbose warning callback.  When the
            ls-remote fallback resolves the ref but the path could not
            be probed, a single ``[!]`` warning is emitted here so users
            in default-verbosity mode still see that path validation
            was deferred to install-time.

    Returns:
        True if the package exists / is accessible, False otherwise.
    """
    if not dep_ref.is_virtual:
        raise ValueError("Can only validate virtual packages with this method")

    ref = dep_ref.reference or "main"
    vpath = dep_ref.virtual_path

    def _log(msg: str) -> None:
        if verbose_callback:
            verbose_callback(msg)

    def _probe(path: str) -> bool:
        try:
            downloader.download_raw_file(dep_ref, path, ref)
            _log(f"  [+] {path}@{ref}")
            return True
        except RuntimeError as exc:
            # Marker-file misses on the success path are expected, not
            # errors -- reserve [x] for genuine failures.  See panel
            # finding 3 on PR #941.
            _log(f"  [i] {path}@{ref} ({exc})")
            return False

    _log(f'  [i] Validating virtual package at ref "{ref}": {dep_ref.repo_url}/{vpath}')

    if dep_ref.is_virtual_collection():
        return _probe(f"{vpath}.collection.yml")

    if dep_ref.is_virtual_file():
        return _probe(vpath)

    if dep_ref.is_virtual_subdirectory():
        marker_paths = [
            f"{vpath}/apm.yml",
            f"{vpath}/SKILL.md",
            f"{vpath}/plugin.json",
            f"{vpath}/.github/plugin/plugin.json",
            f"{vpath}/.claude-plugin/plugin.json",
            f"{vpath}/.cursor-plugin/plugin.json",
            f"{vpath}/README.md",
        ]
        for marker_path in marker_paths:
            if _probe(marker_path):
                return True

        # Fallback 1: directory-exists probe via Contents API.
        if _directory_exists_at_ref(downloader, dep_ref, vpath, ref, _log):
            return True

        # Fallback 2: explicit ref + git ls-remote.  Mirrors install's
        # auth chain so we accept packages whose API auth is stricter
        # than their git auth.  Only kicks in with an explicit ref --
        # without one, strict validation keeps path typos failing fast
        # on the default branch.
        if dep_ref.reference is not None and _ref_exists_via_ls_remote(
            downloader, dep_ref, ref, _log
        ):
            _log(f'  [+] ref "{ref}" resolves via ls-remote; deferring path validation to install')
            if warn_callback is not None:
                warn_callback(
                    "[!] path validation deferred to install (API probe "
                    "inconclusive); use --verbose to see probe detail"
                )
            return True
        return False

    return _probe(vpath)


def _directory_exists_at_ref(
    downloader: GitHubPackageDownloader,
    dep_ref: DependencyReference,
    path: str,
    ref: str,
    log: Callable[[str], None],
) -> bool:
    """Check if a directory exists at ``ref`` via the Contents API.

    Uses the default ``Accept: application/vnd.github+json`` so the
    endpoint returns the directory listing for directories (and file
    metadata for files).  A 200 means the path resolves at the ref,
    which is what install needs.

    Returns ``True`` on 200; ``False`` on 404 or any error.  Only
    implemented for github.com / GHE; non-GitHub hosts return ``False``
    and rely on the marker-file probes above.
    """
    host = dep_ref.host or default_host()
    if dep_ref.is_azure_devops() or not is_github_hostname(host):
        log(f"  [i] directory-exists probe skipped (host {host} not supported)")
        return False

    owner, repo = dep_ref.repo_url.split("/", 1)
    token = downloader.auth_resolver.resolve(host, owner, port=dep_ref.port).token

    encoded_path = quote(path, safe="/")
    encoded_ref = quote(ref, safe="")

    host_lc = host.lower()
    if host_lc == "github.com":
        api_url = (
            f"https://api.github.com/repos/{owner}/{repo}/contents/{encoded_path}?ref={encoded_ref}"
        )
    elif host_lc.endswith(".ghe.com"):
        api_url = (
            f"https://api.{host}/repos/{owner}/{repo}/contents/{encoded_path}?ref={encoded_ref}"
        )
    else:
        api_url = (
            f"https://{host}/api/v3/repos/{owner}/{repo}/contents/{encoded_path}?ref={encoded_ref}"
        )

    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"token {token}"

    try:
        response = downloader._resilient_get(api_url, headers=headers, timeout=30)
        if response.status_code == 200:
            log(f"  [+] {path}@{ref} (directory)")
            return True
        # 404 is the expected "not present at this ref" outcome -- the
        # marker-file fallback path treats this as informational, not
        # an error.  Reserve [x] for unexpected HTTP statuses.
        if response.status_code == 404:
            log(f"  [i] {path}@{ref} (HTTP 404)")
        else:
            log(f"  [x] {path}@{ref} (HTTP {response.status_code})")
        return False
    except (requests.exceptions.RequestException, RuntimeError) as exc:
        log(f"  [x] {path}@{ref} ({exc})")
        return False


def _ref_exists_via_ls_remote(
    downloader: GitHubPackageDownloader,
    dep_ref: DependencyReference,
    ref: str,
    log: Callable[[str], None],
) -> bool:
    """Check if ``ref`` exists in the remote repo via ``git ls-remote``.

    Lenient fallback for when the Contents API rejects a path with 404
    even though ``git clone`` would succeed -- e.g. SSO-half-authorized
    PATs, fine-grained PAT scope mismatches between API and git
    protocols, or repo policies that gate the Contents API more
    strictly than git.

    Mirrors the auth chain in ``_clone_with_fallback``:

    1. Authenticated HTTPS -- explicit PAT in ``downloader.git_env``
       (silences credential helpers via ``GIT_ASKPASS=echo``).
    2. Plain HTTPS w/ credential helper -- token stripped from the URL,
       relaxed env, so the user's git credential helper resolves the
       credential install ultimately uses.
    3. SSH -- only when the user signaled SSH is acceptable (``--ssh``
       or ``--allow-protocol-fallback``).  Wrapped in
       ``ssh -o BatchMode=yes -o ConnectTimeout=10`` so it never hangs
       on a passphrase prompt.

    For SHA-pinned refs (hex-only, 7-40 chars) the ls-remote call omits
    ``--heads --tags`` because those filters silently drop commit SHAs
    -- the full ref list is scanned for a SHA-prefix match instead.
    See panel finding 1 on PR #941.

    Returns ``True`` on the first attempt that resolves the ref;
    ``False`` if every attempt fails.
    """
    if dep_ref.is_artifactory():
        return False

    dep_token = downloader._resolve_dep_token(dep_ref)
    dep_auth_ctx = downloader._resolve_dep_auth_ctx(dep_ref)
    dep_auth_scheme = dep_auth_ctx.auth_scheme if dep_auth_ctx else "basic"
    is_insecure = dep_ref.is_insecure

    attempts: list = []

    # Attempt 1: explicit PAT, locked-down env. Skipped when no token.
    if dep_token:
        token_env = (
            dep_auth_ctx.git_env
            if dep_auth_scheme == "bearer" and dep_auth_ctx is not None
            else downloader.git_env
        )
        token_url = downloader._build_repo_url(
            dep_ref.repo_url,
            use_ssh=False,
            dep_ref=dep_ref,
            token=dep_token,
            auth_scheme=dep_auth_scheme,
        )
        attempts.append(("authenticated HTTPS", token_url, token_env))

    # Attempt 2: plain HTTPS w/ credential helper.
    plain_env = downloader._build_noninteractive_git_env(
        preserve_config_isolation=is_insecure,
        suppress_credential_helpers=is_insecure,
    )
    plain_url = downloader._build_repo_url(
        dep_ref.repo_url,
        use_ssh=False,
        dep_ref=dep_ref,
        token="",
    )
    attempts.append(("plain HTTPS w/ credential helper", plain_url, plain_env))

    # Attempt 3 (SSH): only when allowed.
    if not is_insecure and _ssh_attempt_allowed(downloader):
        try:
            ssh_url = downloader._build_repo_url(
                dep_ref.repo_url,
                use_ssh=True,
                dep_ref=dep_ref,
            )
            ssh_env = dict(plain_env)
            ssh_env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o ConnectTimeout=10"
            attempts.append(("SSH", ssh_url, ssh_env))
        except Exception as exc:
            log(f"  [i] SSH URL build skipped: {exc}")

    is_sha = _is_sha_pin(ref)
    ref_lc = ref.lower()
    g = git.cmd.Git()
    for label, url, env in attempts:
        try:
            if is_sha:
                # SHA pins: scan the full advertised-refs list.  The
                # ``--heads --tags`` filters scan only ``refs/heads/*``
                # and ``refs/tags/*`` and silently drop commit SHAs.
                output = g.ls_remote(url, env=env)
                if output and any(
                    line.split("\t", 1)[0].lower().startswith(ref_lc)
                    for line in output.splitlines()
                    if line
                ):
                    log(f"  [+] ls-remote ok via {label} (SHA match)")
                    return True
                log(f"  [i] ls-remote returned no SHA match via {label}")
            else:
                output = g.ls_remote("--heads", "--tags", url, ref, env=env)
                if output and output.strip():
                    log(f"  [+] ls-remote ok via {label}")
                    return True
                log(f"  [i] ls-remote returned no matching refs via {label}")
        except (GitCommandError, OSError) as exc:
            log(f"  [x] ls-remote failed via {label}: {downloader._sanitize_git_error(str(exc))}")

    return False


def _ssh_attempt_allowed(downloader: GitHubPackageDownloader) -> bool:
    """Whether the SSH ls-remote attempt should run.

    Mirrors ``_clone_with_fallback``'s gating: SSH is in scope when the
    user explicitly preferred it (``--ssh``) or when cross-protocol
    fallback is allowed.  Default HTTPS-preferring users get no SSH
    attempt -- keeps validation output clean and never invokes ssh on
    machines that don't have it configured.
    """
    try:
        from .transport_selection import ProtocolPreference
    except ImportError:
        return False
    return downloader._protocol_pref == ProtocolPreference.SSH or downloader._allow_fallback
