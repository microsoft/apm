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

Git-protocol helpers (auth-chain construction, ls-remote fallback,
shallow-fetch tree probe, SSH gate) live in the sibling private module
``_gh_validation_git_probes`` and are re-exported here so all existing
import paths and ``patch.object`` targets remain unchanged.
"""

from __future__ import annotations

import contextlib
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import git
import requests
from git.exc import GitCommandError

from ..config import get_apm_temp_dir
from ..utils.github_host import (
    default_host,
    is_github_hostname,
)
from ..utils.path_security import (
    PathTraversalError,
    safe_rmtree,
    validate_path_segments,
)

# Re-exported from the git-probes sibling module.  All names are bound
# here so that existing import paths, ``patch.object(gdv, "<name>", ...)``
# calls in tests, and deferred ``from ..github_downloader_validation
# import <name>`` calls in transport_plan.py continue to work unchanged.
from ._gh_validation_git_probes import (  # noqa: F401
    AttemptSpec,
    _build_validation_attempts,
    _is_sha_pin,
    _ref_exists_via_ls_remote,
    _ssh_attempt_allowed,
)

if TYPE_CHECKING:
    from ..models.dependency.reference import DependencyReference
    from .github_downloader import GitHubPackageDownloader


def _split_owner_repo(repo_url: str) -> tuple[str, str] | None:
    """Split ``owner/repo`` safely; return ``None`` if the shape is wrong.

    Guards against ``ValueError`` on tuple-unpacking when ``repo_url``
    has no ``/`` (panel round-2 finding 2).
    """
    parts = repo_url.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]


@dataclass(frozen=True, slots=True)
class _TreeProbeCtx:
    dep_ref: DependencyReference
    vpath: str
    ref: str


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
        warn_callback: Optional non-verbose warning callback. Fired
            when the ls-remote + shallow-fetch fallback resolves both
            the ref and the path. Yellow-traffic-light signal: the
            git-credential chain validated a package the API check
            could not, which may indicate a credential-scope mismatch
            an operator must see in default-verbosity CI runs.

    Returns:
        True if the package exists / is accessible, False otherwise.
    """
    if not dep_ref.is_virtual:
        raise ValueError("Can only validate virtual packages with this method")

    ref: str = dep_ref.reference or "main"
    vpath: str = dep_ref.virtual_path

    # SECURITY (round-2 finding 7 + round-3 finding 2): reject traversal
    # segments before any URL interpolation, and reject empty vpath
    # outright. Empty vpath is not a traversal but `git ls-tree
    # FETCH_HEAD ""` is implementation-defined; some git versions emit a
    # root listing and falsely validate any successfully-fetched repo.
    # `reject_empty=True` closes that hole at the entry point.
    try:
        validate_path_segments(vpath, context="virtual path", reject_empty=True)
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

    if dep_ref.is_virtual_file():
        return _probe(vpath)

    if dep_ref.is_virtual_subdirectory():
        ctx = _TreeProbeCtx(dep_ref=dep_ref, vpath=vpath, ref=ref)
        return _run_subdirectory_validation(downloader, ctx, _probe, _log, warn_callback)

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
    token = downloader.auth_resolver.resolve_for_dep(dep_ref).token

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


def _run_subdirectory_validation(
    downloader: GitHubPackageDownloader,
    ctx: _TreeProbeCtx,
    probe: Callable[[str], bool],
    log: Callable[[str], None],
    warn_callback: Callable[[str], None] | None,
) -> bool:
    """Validate a subdirectory virtual package via marker-file probes and fallbacks.

    Probe order: apm.yml first (a ``collections/<name>/apm.yml`` is the
    supported way to express a curated dependency aggregator -- see
    microsoft/apm#1094), then the standard primitive markers.

    Extracted from ``validate_virtual_package_exists`` to keep McCabe
    complexity under the project threshold.
    """
    marker_paths = [
        f"{ctx.vpath}/apm.yml",
        f"{ctx.vpath}/SKILL.md",
        f"{ctx.vpath}/plugin.json",
        f"{ctx.vpath}/.github/plugin/plugin.json",
        f"{ctx.vpath}/.claude-plugin/plugin.json",
        f"{ctx.vpath}/.cursor-plugin/plugin.json",
        f"{ctx.vpath}/README.md",
    ]
    for marker_path in marker_paths:
        if probe(marker_path):
            return True

    # Fallback 1: directory-exists probe via Contents API.
    if _directory_exists_at_ref(downloader, ctx.dep_ref, ctx.vpath, ctx.ref, log):
        return True

    # Fallback 2: explicit ref + git ls-remote + shallow-fetch path probe.
    # Mirrors install's auth chain so we accept packages whose API auth is
    # stricter than their git auth.  Only kicks in with an explicit,
    # NON-EMPTY ref -- without one, strict validation keeps path typos
    # failing fast on the default branch. Round-3 finding 1: a bare `#`
    # fragment produces `reference == ""`, which `is not None` would let
    # through; the truthy check below rejects it so the fallback is
    # reachable only for explicitly-pinned refs.
    if ctx.dep_ref.reference:
        ref_ok, winning_attempt = _ref_exists_via_ls_remote(downloader, ctx.dep_ref, ctx.ref, log)
        if ref_ok and winning_attempt is not None:
            # SECURITY (round-2 finding 6): close the fail-open.  ls-remote
            # only confirms the ref exists; we MUST also confirm the
            # subdirectory exists at that ref via a shallow-fetch + ls-tree
            # probe, otherwise a typo'd vpath silently passes validation.
            # Reuse the WINNING attempt (panel round-3 auth-chain bug fix)
            # so we don't fall back to attempts[0].
            if _path_exists_in_tree_at_ref(downloader, ctx, log, winning_attempt):
                log(f'  [+] "{ctx.vpath}@{ctx.ref}" confirmed via shallow-fetch + ls-tree')
                if warn_callback is not None:
                    # devx-ux + cli-logging (round-3): name the
                    # security-relevant outcome explicitly. A scoped PAT
                    # may have *correctly* rejected this package on the API
                    # surface; the operator must be able to distinguish that
                    # from a legitimate API hit.
                    warn_callback(
                        f"API validation skipped for {ctx.dep_ref.to_canonical()}; "
                        "resolved via git credential fallback. "
                        "Run with --verbose for details."
                    )
                return True
            log(
                f'  [!] ref "{ctx.ref}" resolves but "{ctx.vpath}" '
                "not present in the tree at that ref"
            )
            return False
    return False


def _path_exists_in_tree_at_ref(
    downloader: GitHubPackageDownloader,
    ctx: _TreeProbeCtx,
    log: Callable[[str], None],
    winning_attempt: AttemptSpec,
) -> bool:
    """Confirm ``ctx.vpath`` exists at ``ctx.ref`` via shallow fetch + ``ls-tree``.

    Closes the fail-open hole in ``_ref_exists_via_ls_remote``: knowing
    that the ref exists is not the same as knowing the subdirectory
    exists at that ref.  This helper initialises a temporary bare repo,
    fetches a single commit with ``--filter=tree:0`` (no blob bodies,
    cheap), and then runs ``ls-tree`` to assert the path is present in
    the resolved tree.  Cleans up the temp dir regardless of outcome.

    Args:
        winning_attempt: The AttemptSpec returned by
            ``_ref_exists_via_ls_remote`` -- MUST be reused so the same
            credential that proved the ref exists is the one used to
            fetch the tree.  Panel round-3 closed the
            ``attempts[0]``-only bug here.

    Returns:
        True iff the shallow fetch succeeded AND ``ls-tree`` reported
        at least one entry for ``ctx.vpath`` at the resolved ref.
    """
    label, url, env = winning_attempt

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
                ctx.ref,
                env=env,
            )
        except (GitCommandError, OSError) as exc:
            log(
                f"  [x] shallow fetch failed via {label}: "
                f"{downloader._sanitize_git_error(str(exc))}"
            )
            return False

        try:
            output = g.ls_tree("FETCH_HEAD", ctx.vpath, env=env)
        except (GitCommandError, OSError) as exc:
            log(f"  [x] ls-tree failed via {label}: {downloader._sanitize_git_error(str(exc))}")
            return False

        if output and output.strip():
            log(f"  [+] {ctx.vpath}@{ctx.ref} present in tree")
            return True
        log(f"  [!] {ctx.vpath} not present in tree at {ctx.ref}")
        return False
    finally:
        # safe_rmtree wraps robust_rmtree with an ensure_path_within
        # containment assertion -- never call robust_rmtree directly.
        with contextlib.suppress(Exception):
            safe_rmtree(tmpdir, base_temp)
