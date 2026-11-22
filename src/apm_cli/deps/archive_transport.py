"""Single-request archive transport for github.com whole-repository dependencies.

A cold ``git clone`` of one dependency costs a TLS connect, the smart-HTTP
ref advertisement, a pack negotiation and about a dozen git subprocesses.
For a public github.com repository the same bytes are available as ONE
``codeload.github.com/{owner}/{repo}/tar.gz/{sha}`` response.

The archive is accepted only when it is provably the git tree of the commit:

- the GitHub API ``git/trees/{sha}`` response names the commit's tree SHA
  (fetched concurrently, so it adds no round trip to the critical path);
- the archive's ``pax`` comment names the requested commit;
- the git tree hash recomputed from the archive members (path, exec bit and
  blob bytes) equals that tree SHA.

Anything else -- a symlink, a submodule, a ``.gitattributes`` file (which can
make a checkout differ from the stored blobs), an export-ignored path, a
size cap, any HTTP or network failure -- raises :class:`ArchiveUnavailable`
and the caller falls back to the git transport. The fetched tree therefore
has the bytes a git checkout writes, so the lockfile content hash is the same
whichever transport ran.

Only anonymous public access is attempted (the same first attempt the git
transport makes for github.com); private repositories, other hosts, SSH,
registry-only mode and ``APM_ARCHIVE_TRANSPORT=0`` keep the git transport.

A verified tree lands in the persistent git cache's own full-tree shard
(``GitCache.land_tree_checkout``), so later cache hits treat it exactly like
a cloned checkout.
"""

from __future__ import annotations

import hashlib
import http.cookiejar
import json
import logging
import os
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import requests
from requests.adapters import HTTPAdapter

from ..utils.archive import read_tar_gz_members, safe_extract_tar_gz_bytes
from ..utils.http_credentials import (
    CredentialScopedSession,
    RedirectRefusedError,
    origin_bounded_request,
)
from ..utils.path_security import ensure_path_within

if TYPE_CHECKING:
    from ..cache.git_cache import GitCache

_log = logging.getLogger(__name__)

ARCHIVE_TRANSPORT_ENV = "APM_ARCHIVE_TRANSPORT"
CODELOAD_HOST = "codeload.github.com"
_CONNECT_TIMEOUT_S = 10
_READ_TIMEOUT_S = 60
_POOL_SIZE = 32
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_TRAILING_DOTS_SPACES = re.compile(r"[. ]+$")
# Refused on every host (an archive is OS-independent): path separators, NUL,
# and ':' (an NTFS alternate data stream on Windows).
_FORBIDDEN_CHARS = ("\\", "\x00", ":")
# Code points HFS+ ignores in names; Git (core.protectHFS) folds them away
# before comparing a segment with ".git", so the archive does too.
_HFS_IGNORABLE = re.compile("[\u200c-\u200f\u202a-\u202e\u206a-\u206f\ufeff]")
_DOS_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)
_GITATTRIBUTES = ".gitattributes"
_TREES_BODY_CAP = 1024 * 1024
_SCRATCH = ".apm-archive"


@dataclass(frozen=True)
class ArchiveLimits:
    """Decompression bounds: hitting any of them falls back to git, nothing lands."""

    compressed_bytes: int = 64 * 1024 * 1024
    uncompressed_bytes: int = 256 * 1024 * 1024  # total of all members
    entries: int = 10_000  # members, directories included


DEFAULT_LIMITS = ArchiveLimits()


class ArchiveUnavailable(Exception):
    """The archive cannot stand in for a git checkout; use the git transport."""


def archive_transport_enabled() -> bool:
    """Default on; ``APM_ARCHIVE_TRANSPORT=0`` keeps every fetch on git."""
    value = os.environ.get(ARCHIVE_TRANSPORT_ENV, "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


# -- git object hashing -------------------------------------------------------


@dataclass(frozen=True)
class TreeEntry:
    """One tracked file: POSIX path relative to the repository root."""

    path: str
    executable: bool
    blob: str


def git_blob_sha(data: bytes) -> str:
    """Return the git (SHA-1) object id of a blob holding *data*."""
    digest = hashlib.sha1(usedforsecurity=False)
    digest.update(b"blob %d\0" % len(data))
    digest.update(data)
    return digest.hexdigest()


def git_tree_sha(entries: list[TreeEntry]) -> str:
    """Return the git tree id of a tree holding exactly *entries* (files only)."""
    root: dict[str, object] = {}
    for entry in entries:
        parts = entry.path.split("/")
        node = root
        for part in parts[:-1]:
            child = node.setdefault(part, {})
            if not isinstance(child, dict):
                raise ArchiveUnavailable(f"path is both a file and a directory: {entry.path}")
            node = child
        if parts[-1] in node:
            raise ArchiveUnavailable(f"duplicate path: {entry.path}")
        node[parts[-1]] = entry
    return _hash_tree_node(root)


def _hash_tree_node(node: dict[str, object]) -> str:
    rows: list[tuple[bytes, bytes]] = []
    for name, child in node.items():
        raw = name.encode("utf-8", "surrogateescape")
        if isinstance(child, dict):
            # Git orders a directory as if its name ended with "/".
            rows.append(
                (raw + b"/", b"40000 " + raw + b"\0" + bytes.fromhex(_hash_tree_node(child)))
            )
        elif isinstance(child, TreeEntry):
            mode = b"100755" if child.executable else b"100644"
            rows.append((raw, mode + b" " + raw + b"\0" + bytes.fromhex(child.blob)))
    body = b"".join(row for _, row in sorted(rows))
    digest = hashlib.sha1(usedforsecurity=False)
    digest.update(b"tree %d\0" % len(body))
    digest.update(body)
    return digest.hexdigest()


# -- HTTP ---------------------------------------------------------------------

_SESSION: requests.Session | None = None
_SESSION_LOCK = threading.Lock()
_DISABLED_REASON: str | None = None


def shared_session() -> requests.Session:
    """One keep-alive session per process: connections are reused across deps.

    A :class:`CredentialScopedSession` without ambient credentials: proxy and
    CA settings follow the environment, netrc is never read and cookies are
    never stored. Requests go through ``origin_bounded_request`` with no
    redirect hop, so nothing is followed off the requested URL.
    """
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is None:
            session = CredentialScopedSession(ambient_credentials=False)
            session.cookies.set_policy(http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))
            session.mount("https://", HTTPAdapter(pool_maxsize=_POOL_SIZE))
            _SESSION = session
        return _SESSION


def reset_run_state() -> None:
    """Forget an earlier unreachable-codeload verdict and the pooled session."""
    global _DISABLED_REASON, _SESSION
    with _SESSION_LOCK:
        _SESSION = None
    _DISABLED_REASON = None


def _disable(reason: str) -> None:
    """Stop trying archives for the rest of the run (e.g. codeload unreachable)."""
    global _DISABLED_REASON
    _DISABLED_REASON = reason
    _log.debug("Archive transport disabled for this run: %s", reason)


def _get(url: str, headers: dict[str, str]) -> requests.Response:
    try:
        return origin_bounded_request(
            shared_session(),
            "GET",
            url,
            base_url=url,
            headers=headers,
            max_hops=0,
            ambient_credentials=False,
            timeout=(_CONNECT_TIMEOUT_S, _READ_TIMEOUT_S),
            stream=True,
        )
    except RedirectRefusedError as exc:
        _release(exc.response)
        raise ArchiveUnavailable("redirect refused") from exc
    except (requests.exceptions.RequestException, OSError) as exc:
        raise _network_failure(exc, url) from exc


def _network_failure(exc: BaseException, url: str) -> ArchiveUnavailable:
    """One policy for every transport error: git for this dependency and the rest of the run.

    Connect, TLS, proxy and read failures (before or while streaming a body)
    would most likely repeat for every later dependency, each paying the
    timeout again, so archives are disabled for the run.
    """
    host = url.split("/")[2] if url.count("/") >= 2 else url
    _disable(f"{type(exc).__name__} for {host}")
    return ArchiveUnavailable(f"network failure: {type(exc).__name__}")


def _release(response: requests.Response | None) -> None:
    """Release a streamed response's pooled connection; cleanup never masks the refusal."""
    if response is None:
        return
    try:
        response.close()
    except (AttributeError, OSError, requests.exceptions.RequestException):
        _log.debug("Could not release an unfollowed redirect response")


def _read_capped(response: requests.Response, cap: int) -> bytes:
    """Read the streamed body under *cap*; a transport error mid-stream disables archives."""
    declared = response.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > cap:
        response.close()
        raise ArchiveUnavailable("archive larger than the archive transport cap")
    chunks: list[bytes] = []
    total = 0
    try:
        for chunk in response.iter_content(1024 * 1024):
            total += len(chunk)
            if total > cap:
                raise ArchiveUnavailable("archive larger than the archive transport cap")
            chunks.append(chunk)
    except (requests.exceptions.RequestException, OSError) as exc:
        raise _network_failure(exc, str(getattr(response, "url", "") or "")) from exc
    finally:
        _release(response)
    return b"".join(chunks)


def _fetch_tree_sha(api_base: str, owner: str, repo: str, sha: str, headers: dict) -> str:
    response = _get(f"{api_base}/repos/{owner}/{repo}/git/trees/{sha}", headers)
    try:
        if response.status_code in (403, 429):
            # Quota or abuse limit: every later dependency would hit it too.
            _disable(f"trees API returned HTTP {response.status_code}")
        if response.status_code != 200:
            raise ArchiveUnavailable(f"trees API returned HTTP {response.status_code}")
        try:
            tree = json.loads(_read_capped(response, _TREES_BODY_CAP)).get("sha", "")
        except (ValueError, AttributeError) as exc:
            raise ArchiveUnavailable("trees API returned an unexpected body") from exc
    finally:
        response.close()
    tree = str(tree).lower()
    if not _SHA1_RE.match(tree):
        raise ArchiveUnavailable("trees API returned no SHA-1 tree id")
    return tree


def _fetch_archive(owner: str, repo: str, sha: str, limits: ArchiveLimits) -> bytes:
    response = _get(f"https://{CODELOAD_HOST}/{owner}/{repo}/tar.gz/{sha}", {})
    if response.status_code != 200:
        response.close()
        raise ArchiveUnavailable(f"archive returned HTTP {response.status_code}")
    return _read_capped(response, limits.compressed_bytes)


# -- archive verification -----------------------------------------------------


def _archive_entries(
    data: bytes, commit: str, limits: ArchiveLimits = DEFAULT_LIMITS
) -> tuple[str, list[TreeEntry]]:
    """Return ``(prefix, entries)`` of a git archive of *commit*, or raise.

    Hashes every regular member; refuses links, special files, a missing or
    different commit id, more than one top-level directory, non-canonical
    paths and any ``.gitattributes`` (attributes can make a checkout differ
    from the stored blobs).
    """
    headers, members = read_tar_gz_members(
        data,
        max_entries=limits.entries,
        max_uncompressed=limits.uncompressed_bytes,
        error_type=ArchiveUnavailable,
    )
    entries: list[TreeEntry] = []
    directories: set[str] = set()
    prefix: str | None = None
    for name, mode, content in members:
        # Validate the raw name before any normalization, for every member kind.
        raw = name.removesuffix("/") if content is None else name
        parts = _canonical_parts(raw, name)
        if prefix is None:
            prefix = parts[0]
        if parts[0] != prefix:
            raise ArchiveUnavailable("archive has more than one top-level directory")
        rest = "/".join(parts[1:])
        if not rest:
            continue
        if content is None:
            directories.add(rest)
            continue
        if parts[-1].casefold() == _GITATTRIBUTES:
            raise ArchiveUnavailable("repository has .gitattributes")
        entries.append(TreeEntry(rest, bool(mode & 0o111), git_blob_sha(content)))
    # A git tree has no empty directories; one would land unverified and could
    # change what the package looks like (e.g. a bare ``.apm/``).
    ancestors = {e.path.rsplit("/", i)[0] for e in entries for i in range(1, e.path.count("/") + 1)}
    if directories - ancestors:
        raise ArchiveUnavailable("archive has a directory outside the tree")
    if headers.get("comment", "").strip().lower() != commit:
        raise ArchiveUnavailable("archive does not name the requested commit")
    if prefix is None:
        raise ArchiveUnavailable("empty archive")
    return prefix, entries


def _canonical_parts(path: str, name: str) -> list[str]:
    """The segments of member *path*, or raise unless it is a canonical, checkout-safe path.

    No empty, ``.`` or ``..`` segment, backslash, NUL or ``:`` (NTFS stream),
    no trailing dot or space and no DOS device name (``CON``, ``NUL``,
    ``COM1``..., with any extension), since Windows would write those under
    another name or not at all; and no segment Git itself refuses to check out
    (``.git`` in any case, its 8.3 alias ``git~1``) or that the landing uses.
    Rules apply on every host: the archive is the same everywhere.
    """
    parts = path.split("/")
    for part in parts:
        if part in ("", ".", "..") or any(char in part for char in _FORBIDDEN_CHARS):
            raise ArchiveUnavailable(f"non-canonical archive path: {name!r}")
        if part.endswith((".", " ")):
            # Win32 drops trailing dots/spaces: the file would land under another name.
            raise ArchiveUnavailable(f"non-portable archive path: {name!r}")
        stem = _TRAILING_DOTS_SPACES.sub("", part.split(".", 1)[0]).casefold()
        if stem in _DOS_DEVICE_NAMES:
            raise ArchiveUnavailable(f"non-portable archive path: {name!r}")
        if _HFS_IGNORABLE.sub("", part).casefold() in (".git", "git~1", _SCRATCH):
            raise ArchiveUnavailable(f"reserved path in archive: {name!r}")
    return parts


def _extract(data: bytes, prefix: str, staging: Path, limits: ArchiveLimits) -> Path:
    safe_extract_tar_gz_bytes(
        data,
        staging,
        max_entries=limits.entries,
        max_uncompressed=limits.uncompressed_bytes,
        error_type=ArchiveUnavailable,
    )
    root = staging / prefix
    ensure_path_within(root, staging)
    return root


# -- landing ------------------------------------------------------------------


def _verify_files(root: Path, entries: list[TreeEntry]) -> None:
    """Every file under *root* is exactly one tree entry, with its blob bytes."""
    expected = {e.path: e.blob for e in entries}
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            if (Path(dirpath) / name).is_symlink():
                raise ArchiveUnavailable(f"unexpected link in archive tree: {name}")
        for name in filenames:
            full = Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            if expected.get(rel) != git_blob_sha(full.read_bytes()):
                raise ArchiveUnavailable(f"content does not match the tree: {rel}")
            seen += 1
    if seen != len(expected):
        raise ArchiveUnavailable("archive tree is missing files")


def _populate_from(data: bytes, prefix: str, entries: list[TreeEntry], limits: ArchiveLimits):
    """Return a callback writing the verified archive files into a staging dir."""

    def populate(dest: Path) -> None:
        scratch = dest / _SCRATCH
        root = _extract(data, prefix, scratch, limits)
        for child in root.iterdir():
            os.replace(child, dest / child.name)
        shutil.rmtree(scratch)
        _verify_files(dest, entries)
        # The exec bit is part of the verified tree: apply it, never the archive mode.
        for entry in entries:
            os.chmod(dest / entry.path, 0o755 if entry.executable else 0o644)

    return populate


# -- entry point ----------------------------------------------------------------


def fetch_verified_tree(
    cache: GitCache,
    *,
    url: str,
    api_base: str,
    owner: str,
    repo: str,
    commit: str,
    api_headers: dict[str, str],
    limits: ArchiveLimits = DEFAULT_LIMITS,
) -> Path:
    """Land a verified tree of *commit* in *cache* from one archive request.

    Returns the full-tree checkout shard. Raises :class:`ArchiveUnavailable`
    whenever the git transport must run instead; nothing is landed then.
    """
    commit = commit.lower()
    if not _SHA1_RE.match(commit):
        raise ArchiveUnavailable("archive transport needs a full SHA-1 commit")
    if _DISABLED_REASON is not None:
        raise ArchiveUnavailable(_DISABLED_REASON)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="apm-tree") as pool:
        tree_future = pool.submit(_fetch_tree_sha, api_base, owner, repo, commit, api_headers)
        try:
            data = _fetch_archive(owner, repo, commit, limits)
        finally:
            tree_sha = tree_future.result()
    prefix, entries = _archive_entries(data, commit, limits)
    if git_tree_sha(entries) != tree_sha:
        raise ArchiveUnavailable("archive content does not match the commit's tree")
    populate = _populate_from(data, prefix, entries, limits)
    return cache.land_tree_checkout(url, commit, tree_sha, populate)
