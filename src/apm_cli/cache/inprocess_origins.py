"""Which origins the in-process pinned fetch may serve, and what each requires (W3d EH1-EH10).

github.com and GHE Cloud (``*.ghe.com``) run GitHub's server stack, which
rejects SHA-1 collision objects at push; GHES and GitLab cannot be assumed
to, so their packs also pass one ``git index-pack --stdin`` (git's sha1dc)
before anything is written (EH1). Every other host is git's.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass

_GITHUB_HOST = "github.com"
# Host kind -> the Basic user APM's managed header carries for it (EH3).
_MANAGED_USERS = {
    "github": b"x-access-token:",
    "ghe_cloud": b"x-access-token:",
    "ghes": b"x-access-token:",
    "gitlab": b"oauth2:",
}
_COLLISION_CHECKED_KINDS = frozenset({"ghes", "gitlab"})


class OriginRefused(Exception):
    """This URL's origin, or what it needs, is git's."""


@dataclass(frozen=True)
class Origin:
    kind: str
    host: str
    upload_url: str

    @property
    def enterprise(self) -> bool:
        return self.kind != "github"

    @property
    def probe_url(self) -> str:
        return f"https://{self.host}/apm/probe"

    @property
    def managed_user(self) -> bytes:
        return _MANAGED_USERS[self.kind]

    @property
    def checks_collisions(self) -> bool:
        return self.kind in _COLLISION_CHECKED_KINDS


def origin_of(url: str) -> Origin:
    """The classified origin of a plain https clone URL, or :class:`OriginRefused` (EH2)."""
    from ..core.host_providers import classify_host_provider
    from ..deps import anonymous_ref_listing as anon

    parsed = urllib.parse.urlsplit(url)
    if parsed.hostname == _GITHUB_HOST:
        try:
            path = anon._repository_path(url)
        except anon._Fallback as reason:
            raise OriginRefused(str(reason)) from None
        if url != f"https://{_GITHUB_HOST}{path}":
            raise OriginRefused("url is not the validated github.com form")
        return Origin("github", _GITHUB_HOST, f"https://{_GITHUB_HOST}{path}/git-upload-pack")
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or not host
        or parsed.netloc != host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise OriginRefused("not a plain https URL")
    try:
        kind = classify_host_provider(host).kind
    except (ValueError, RuntimeError):
        raise OriginRefused("unclassified host") from None
    if kind not in _MANAGED_USERS or kind == "github":
        raise OriginRefused(f"host kind {kind} is git's")
    segments = parsed.path.split("/")
    if segments[0] != "" or len(segments) < 3 or (kind != "gitlab" and len(segments) != 3):
        raise OriginRefused("not an owner/repo path")
    names = segments[1:]
    last = names[-1]
    names[-1] = last[: -len(".git")] if last.endswith(".git") else last
    for name in names:
        if not anon._NAME.fullmatch(name) or name.startswith(".") or ".." in name:
            raise OriginRefused("path outside the plain name charset")
    # EH5: the verbatim URL git would use, plus the smart-HTTP service path.
    return Origin(kind, host, f"{url}/git-upload-pack")


def require_collision_checked(
    origin: Origin, pack: bytes, child_env: Mapping[str, str], entries
) -> None:
    """EH1: one ``git index-pack --stdin`` of *pack* for GHES/GitLab; any doubt is git's.

    git's sha1dc rejects a collision-attack object; ``--fsck-objects`` runs
    only when the user's ``fetch``/``transfer.fsckObjects`` asks git for it.
    The pack checksum git prints must equal the trailer this module parsed.
    """
    if not origin.checks_collisions:
        return
    from ..utils.git_env import get_git_executable

    if len(pack) < 32:
        raise OriginRefused("pack too short for a trailer")
    command = [get_git_executable(), "index-pack", "--stdin"]
    if _fsck_requested(entries):
        command.append("--fsck-objects")
    with tempfile.TemporaryDirectory(prefix="apm-index-pack-") as git_dir:
        for sub in ("objects/pack", "objects/info", "refs"):
            os.makedirs(os.path.join(git_dir, *sub.split("/")), exist_ok=True)
        with open(os.path.join(git_dir, "HEAD"), "wb") as head:
            head.write(b"ref: refs/heads/main\n")
        env = {**child_env, "GIT_DIR": git_dir}
        env.pop("GIT_WORK_TREE", None)
        try:
            result = subprocess.run(
                command,
                input=pack,
                env=env,
                cwd=git_dir,
                capture_output=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise OriginRefused(f"index-pack did not run: {type(exc).__name__}") from None
    if result.returncode != 0:
        raise OriginRefused("index-pack rejected the pack")
    printed = result.stdout.decode("ascii", "replace").strip().split()
    if len(printed) != 2 or printed[0] != "pack" or printed[1] != pack[-20:].hex():
        raise OriginRefused("index-pack checksum differs from the pack trailer")


def _fsck_requested(entries) -> bool:
    """``fetch.fsckObjects``, else ``transfer.fsckObjects``, as git reads them (last wins)."""
    found: dict[str, str] = {}
    for entry in entries:
        key = entry.key.lower()
        if key in ("fetch.fsckobjects", "transfer.fsckobjects"):
            found[key] = entry.value
    value = found.get("fetch.fsckobjects", found.get("transfer.fsckobjects"))
    # A bare ``fsckObjects`` key is true to git; anything unparsed asks for fsck too.
    return value is not None and value.strip().lower() not in ("0", "false", "no", "off")
