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

Security gates (round-2 panel findings)
---------------------------------------
* ``validate_path_segments`` is invoked at the entry point on the
  user-supplied ``virtual_path`` before any URL interpolation, blocking
  ``..`` traversal segments from leaking into Contents API or archive
  URLs.
* The ``ls-remote`` fallback no longer fails open: a successful
  ``ls-remote`` only proves the *ref* exists, so we additionally
  shallow-fetch + ``ls-tree`` to confirm ``vpath`` resolves at that ref
  before returning ``True``.
* For Azure DevOps, credentials (PAT or AAD bearer) are injected via
  ``http.extraheader`` (see ``build_authorization_header_git_env``) and
  never embedded in the clone URL.  This keeps tokens out of the OS
  process table, git's own logs, and any downstream debug output.
"""

from __future__ import annotations

import contextlib
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import git
import requests
from git.exc import GitCommandError

from ..config import get_apm_temp_dir
from ..utils.file_ops import robust_rmtree
from ..utils.github_host import (
    build_authorization_header_git_env,
    default_host,
    is_github_hostname,
)
from ..utils.path_security import PathTraversalError, validate_path_segments

if TYPE_CHECKING:
    from .dependency_reference import DependencyReference
    from .github_downloader import GitHubPackageDownloader


_SHA_RE = re.compile(r"[0-9a-fA-F]{7,40}")


def _is_sha_pin(ref: str) -> bool:
    """Return True when ``ref`` looks like an abbreviated or full git SHA."""
    return bool(_SHA_RE.fullmatch(ref))


def _split_owner_repo(repo_url: str) -> tuple[str, str] | None:
    """Split ``owner/repo`` safely; return ``None`` if the shape is wrong.

    Guards against ``ValueError`` on tuple-unpacking when ``repo_url``
    has no ``/`` (panel round-2 finding 2).
    """
    parts = repo_url.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]


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
    directory probe, then ``git ls-remote`` + shallow-fetch path probe
    mirroring the install auth chain) catch packages whose API auth is
    stricter than their git auth.  See PR #941 for the auth-alignment
    rationale.

    Args:
        downloader: The ``GitHubPackageDownloader`` instance providing
            transport, auth, and helper methods.
        dep_ref: Parsed dependency reference for the virtual package.
        verbose_callback: Optional per-probe log callback (verbose mode).
        warn_callback: Optional non-verbose warning callback.  Emitted
            when the ls-remote + shallow-fetch fallback resolves both
            the ref and the path, so users in default-verbosity mode
            still see that strict API validation was bypassed.

    Returns:
        True if the package exists / is accessible, False otherwise.
    """
    if not dep_ref.is_virtual:
        raise ValueError("Can only validate virtual packages with this method")

    ref: str = dep_ref.reference or "main"
    vpath: str = dep_ref.virtual_path

    # SECURITY (round-2 finding 7): reject traversal segments before any
    # URL interpolation. Empty / single-dot segments are tolerated only
    # because some legitimate vpaths normalise to them; '..' is hard-rejected.
    try:
        validate_path_segments(vpath, context="virtual path")
    except PathTraversalError as exc:
        if verbose_callback:
            verbose_callback(f"  [x] virtual path rejected: {exc}")
        return False

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
            # errors -- reserve [x] for genuine failures.
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

        # Fallback 2: explicit ref + git ls-remote + shallow-fetch path
        # probe.  Mirrors install's auth chain so we accept packages
        # whose API auth is stricter than their git auth.  Only kicks in
        # with an explicit ref -- without one, strict validation keeps
        # path typos failing fast on the default branch.
        if dep_ref.reference is not None and _ref_exists_via_ls_remote(
            downloader, dep_ref, ref, _log
        ):
            # SECURITY (round-2 finding 6): close the fail-open.  ls-remote
            # only confirms the ref exists; we MUST also confirm the
            # subdirectory exists at that ref via a shallow-fetch +
            # ls-tree probe, otherwise a typo'd vpath silently passes
            # validation.
            if _path_exists_in_tree_at_ref(downloader, dep_ref, vpath, ref, _log):
                _log(f'  [+] "{vpath}@{ref}" confirmed via shallow-fetch + ls-tree')
                if warn_callback is not None:
                    warn_callback(
                        f"Path '{vpath}@{ref}' validated via git fallback "
                        "(API probe was inconclusive). To fix the API gap: "
                        "verify your token's Contents-API scope, or run "
                        "'apm install --verbose' to see the probe chain."
                    )
                return True
            _log(f'  [i] ref "{ref}" resolves but "{vpath}" not present in the tree at that ref')
            return False
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
    host: str = dep_ref.host or default_host()
    if dep_ref.is_azure_devops() or not is_github_hostname(host):
        log(f"  [i] directory-exists probe skipped (host {host} not supported)")
        return False

    parts = _split_owner_repo(dep_ref.repo_url)
    if parts is None:
        log(f"  [x] repo_url '{dep_ref.repo_url}' missing owner/repo split")
        return False
    owner, repo = parts
    token = downloader.auth_resolver.resolve(host, owner, port=dep_ref.port).token

    from urllib.parse import quote

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

    headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
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


def _build_validation_attempts(
    downloader: GitHubPackageDownloader,
    dep_ref: DependencyReference,
    log: Callable[[str], None],
) -> list[tuple[str, str, dict]]:
    """Return the (label, url, env) attempts for a probe against ``dep_ref``.

    Mirrors the auth chain in ``_clone_with_fallback`` and centralises the
    ADO header-injection switch so both ``ls-remote`` and the shallow-fetch
    path probe reuse it.

    SECURITY (round-2 finding 8): for Azure DevOps we inject the token via
    ``http.extraheader`` (``Authorization: Bearer ...``) regardless of
    whether the resolved scheme is ``basic`` (PAT) or ``bearer`` (AAD JWT).
    This keeps tokens out of the OS process table, git's own logs, and the
    URLs that ``_sanitize_git_error`` would otherwise need to scrub.
    """
    if dep_ref.is_artifactory():
        return []

    dep_token: str | None = downloader._resolve_dep_token(dep_ref)
    dep_auth_ctx = downloader._resolve_dep_auth_ctx(dep_ref)
    dep_auth_scheme: str = dep_auth_ctx.auth_scheme if dep_auth_ctx else "basic"
    is_insecure: bool = bool(getattr(dep_ref, "is_insecure", False))
    is_ado: bool = dep_ref.is_azure_devops()

    attempts: list[tuple[str, str, dict]] = []

    # Attempt 1: explicit token, locked-down env. Skipped when no token.
    if dep_token:
        if is_ado:
            # ADO: ALWAYS use header injection, never URL embedding.
            token_env = {
                **downloader.git_env,
                **build_authorization_header_git_env("Bearer", dep_token),
            }
            token_url = downloader._build_repo_url(
                dep_ref.repo_url,
                use_ssh=False,
                dep_ref=dep_ref,
                token="",  # tokenless URL
                auth_scheme="bearer",
            )
            attempts.append(("ADO authenticated HTTPS (bearer header)", token_url, token_env))
        else:
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

    # Attempt 2: plain HTTPS w/ credential helper (no token).
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

    return attempts


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

    For SHA-pinned refs (hex-only, 7-40 chars) the ls-remote call omits
    ``--heads --tags`` because those filters silently drop commit SHAs
    -- the full ref list is scanned for a SHA-prefix match instead.

    Returns ``True`` on the first attempt that resolves the ref;
    ``False`` if every attempt fails.
    """
    attempts = _build_validation_attempts(downloader, dep_ref, log)
    if not attempts:
        return False

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


def _path_exists_in_tree_at_ref(
    downloader: GitHubPackageDownloader,
    dep_ref: DependencyReference,
    vpath: str,
    ref: str,
    log: Callable[[str], None],
) -> bool:
    """Confirm ``vpath`` exists at ``ref`` via shallow fetch + ``ls-tree``.

    Closes the fail-open hole in ``_ref_exists_via_ls_remote``: knowing
    that the ref exists is not the same as knowing the subdirectory
    exists at that ref.  This helper initialises a temporary bare repo,
    fetches a single commit with ``--filter=tree:0`` (no blob bodies,
    cheap), and then runs ``ls-tree`` to assert the path is present in
    the resolved tree.  Cleans up the temp dir regardless of outcome.

    Returns:
        True iff the shallow fetch succeeded AND ``ls-tree`` reported
        at least one entry for ``vpath`` at the resolved ref.
    """
    attempts = _build_validation_attempts(downloader, dep_ref, log)
    if not attempts:
        return False

    # Pick the first attempt that has a token (or fall back to the plain
    # attempt) -- we don't need the full chain here since the ls-remote
    # caller already proved at least one of these works.
    label, url, env = attempts[0]

    base_temp = get_apm_temp_dir()
    tmpdir = Path(tempfile.mkdtemp(prefix="apm-validate-", dir=base_temp))
    try:
        bare = tmpdir / "probe.git"
        bare.mkdir()
        g = git.cmd.Git(str(bare))
        try:
            g.init("--bare")
            g.remote("add", "origin", url)
            # --filter=tree:0 keeps the fetch payload tiny: we get the
            # commit + a single tree object, no blob contents.
            g.fetch(
                "--depth=1",
                "--filter=tree:0",
                "origin",
                ref,
                env=env,
            )
        except (GitCommandError, OSError) as exc:
            log(
                f"  [x] shallow fetch failed via {label}: "
                f"{downloader._sanitize_git_error(str(exc))}"
            )
            return False

        try:
            output = g.ls_tree("FETCH_HEAD", vpath, env=env)
        except (GitCommandError, OSError) as exc:
            log(f"  [x] ls-tree failed via {label}: {downloader._sanitize_git_error(str(exc))}")
            return False

        if output and output.strip():
            log(f"  [+] {vpath}@{ref} present in tree")
            return True
        log(f"  [i] {vpath} not present in tree at {ref}")
        return False
    finally:
        with contextlib.suppress(Exception):
            robust_rmtree(tmpdir)


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
