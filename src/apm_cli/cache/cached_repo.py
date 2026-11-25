"""Git runs on repositories APM keeps between runs.

A persisted repository (a cache bare, a cache checkout, a native snapshot)
can be replaced on disk between runs, and Git always reads a repository's
own config. So before any Git run there, :func:`cached_repo_is_trusted`
parses that config without executing anything and accepts it only when every
key is one APM itself writes, with a value APM would write. Includes, side
config files (``commondir``, ``info/attributes``), active hooks and object
alternates that leave the cache are refused too. A refused repository is
discarded by its caller and rebuilt; it is never used.

Every such Git run also takes :func:`inert_git_args` and
:func:`cached_repo_env`: no hooks, no fsmonitor, no terminal prompt, a
fixed askpass, and no ``ext``/``fd`` transports.
"""

from __future__ import annotations

import logging
import os
import re
import stat
from collections.abc import Callable, Iterator
from pathlib import Path

_log = logging.getLogger(__name__)

_BOOL = frozenset({"true", "false", "yes", "no", "on", "off", "1", "0", ""})
_REFSPEC = re.compile(r"\+?refs/[A-Za-z0-9_./*-]+:refs/[A-Za-z0-9_./*-]+")
# Git's branch-name syntax (git check-ref-format): no colon, so never a URL or a
# refspec; no "..", "//" or "@{", so never a path or a revision expression.
_BRANCH_MERGE = re.compile(r"refs/heads/(?!.*(?:\.\.|//|@\{))[^\x00-\x20\x7f~^:?*\[\\]+")
_APM_VALUE = re.compile(r"[a-z0-9-]+")
_SECTION = re.compile(r'\[([A-Za-z0-9-]+)(?:\s+"((?:[^"\\\n]|\\["\\])*)")?\]')
_SUBSECTION_ESCAPE = re.compile(r'\\(["\\])')
# A ref name as Git spells one (git check-ref-format): components never start
# with "." (so no "..") nor end in ".lock", and never hold "/", "\\", ":" or
# control characters.
_REF_COMPONENT = r"(?!\.)(?![^/]*\.lock(?:/|$))[^\x00-\x20\x7f~^:?*\[\\/]+"
_REF_NAME = re.compile(rf"refs/(?:{_REF_COMPONENT}/)*{_REF_COMPONENT}", re.ASCII)
# Git trims trailing whitespace when it reads HEAD.
_HEAD = re.compile(
    rf"(?:ref:[ \t]*{_REF_NAME.pattern}|[0-9a-f]{{40}}|[0-9a-f]{{64}})[ \t\r\n\v\f]*",
    re.ASCII,
)
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_BINARY", 0)
)
_CONFIG_UNSAFE_EXTRA = frozenset("\x7f\x85\u2028\u2029\ufeff")
_BLANK_EDGES = re.compile(r"[ \t]*(.*?)[ \t]*", re.DOTALL)
_KEY = re.compile(r"([A-Za-z][A-Za-z0-9-]*)\s*(?:=(.*))?")
_ALLOWED_PROTOCOLS = "file:http:https:ssh"

OriginCheck = Callable[[str], bool]


def _bool(value: str) -> bool:
    return value.lower() in _BOOL


# (section, subsection or None, key) -> value predicate. Subsections are
# case-sensitive; "*" stands for any subsection (branch names only).
_CONFIG_RULES: dict[tuple[str, str | None, str], Callable[[str], bool]] = {
    ("core", None, "repositoryformatversion"): lambda v: v in {"0", "1"},
    ("core", None, "filemode"): _bool,
    ("core", None, "bare"): _bool,
    ("core", None, "logallrefupdates"): _bool,
    ("core", None, "ignorecase"): _bool,
    ("core", None, "precomposeunicode"): _bool,
    ("core", None, "symlinks"): _bool,
    ("core", None, "longpaths"): _bool,
    ("core", None, "autocrlf"): lambda v: v.lower() == "false",
    ("core", None, "eol"): lambda v: v.lower() == "lf",
    ("core", None, "sparsecheckout"): _bool,
    ("core", None, "sparsecheckoutcone"): _bool,
    ("index", None, "sparse"): _bool,
    ("extensions", None, "worktreeconfig"): _bool,
    ("extensions", None, "partialclone"): lambda v: v == "origin",
    ("extensions", None, "objectformat"): lambda v: v.lower() in {"sha1", "sha256"},
    ("apm", None, "linkmaterialization"): _APM_VALUE.fullmatch,
    ("apm", None, "sparsematerialization"): _APM_VALUE.fullmatch,
    ("remote", "origin", "fetch"): _REFSPEC.fullmatch,
    ("remote", "origin", "tagopt"): lambda v: v == "--no-tags",
    ("remote", "origin", "promisor"): _bool,
    ("remote", "origin", "partialclonefilter"): lambda v: v in {"blob:none", "tree:0"},
    ("branch", "*", "remote"): lambda v: v == "origin",
    ("branch", "*", "merge"): _BRANCH_MERGE.fullmatch,
}
_WORKTREE_KEYS = frozenset(
    {
        ("core", None, "sparsecheckout"),
        ("core", None, "sparsecheckoutcone"),
        ("index", None, "sparse"),
    }
)


class UntrustedCachedRepoError(RuntimeError):
    """A persisted repository holds state APM never writes."""


def _value(raw: str) -> str:
    """Parse a Git config value: quotes and the escapes Git writes; no continuations."""
    out: list[str] = []
    quoted = False
    pending_space = ""
    chars = iter(raw)
    for char in chars:
        if char == '"':
            quoted = not quoted
            continue
        if char == "\\":
            escaped = next(chars, "")
            mapped = {"\\": "\\", '"': '"', "n": "\n", "t": "\t", "b": "\b"}.get(escaped)
            if mapped is None:
                raise UntrustedCachedRepoError("unsupported escape in config value")
            out.append(pending_space + mapped)
            pending_space = ""
            continue
        if not quoted and char in "#;":
            break
        if not quoted and char in " \t":
            pending_space += char
            continue
        out.append(pending_space + char)
        pending_space = ""
    if quoted:
        raise UntrustedCachedRepoError("unterminated quote in config value")
    return "".join(out)


def _regular_text(path: Path) -> str:
    """The UTF-8 text of *path*, read only as a regular file (never a link, pipe or device)."""
    if not stat.S_ISREG(os.lstat(path).st_mode):
        raise UntrustedCachedRepoError(f"{path.name} is not a regular file")
    try:
        fd = os.open(path, _READ_FLAGS)
    except OSError as error:
        raise UntrustedCachedRepoError(f"{path.name} is not a regular file") from error
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise UntrustedCachedRepoError(f"{path.name} is not a regular file")
        return handle.read().decode("utf-8")


def _config_entries(path: Path) -> Iterator[tuple[str, str | None, str, str]]:
    """Yield ``(section, subsection, key, value)``; refuse anything unusual."""
    text = _regular_text(path)
    section: tuple[str, str | None] | None = None
    # Git splits config lines on "\n" only; anything that would split them
    # differently here is refused rather than interpreted.
    for raw in text.split("\n"):
        raw = raw.removesuffix("\r")
        if any((ord(c) < 0x20 and c != "\t") or c in _CONFIG_UNSAFE_EXTRA for c in raw):
            raise UntrustedCachedRepoError("unsupported character in config")
        line = _BLANK_EDGES.fullmatch(raw).group(1)
        if not line or line[0] in "#;":
            continue
        if line.startswith("["):
            match = _SECTION.fullmatch(line)
            if match is None:
                raise UntrustedCachedRepoError(f"unsupported config section {line!r}")
            subsection = match.group(2)
            if subsection is not None:
                subsection = _SUBSECTION_ESCAPE.sub(r"\1", subsection)
            section = (match.group(1).lower(), subsection)
            continue
        match = _KEY.fullmatch(line)
        if match is None or section is None:
            raise UntrustedCachedRepoError("unsupported config line")
        rest = match.group(2)
        if rest is not None and rest.rstrip().endswith("\\"):
            raise UntrustedCachedRepoError("config line continuation")
        value = "true" if rest is None else _value(rest.strip())
        yield section[0], section[1], match.group(1).lower(), value


def _check_config(path: Path, *, origin_ok: OriginCheck, worktree: bool) -> None:
    for section, subsection, key, value in _config_entries(path):
        if section == "remote" and subsection == "origin" and key == "url" and not worktree:
            if not origin_ok(value):
                raise UntrustedCachedRepoError("remote.origin.url does not name this repository")
            continue
        rule_key = (section, "*" if section == "branch" and subsection else subsection, key)
        rule = _CONFIG_RULES.get(rule_key)
        if rule is None or (worktree and rule_key not in _WORKTREE_KEYS) or not rule(value):
            shown = f"{section}.{subsection}.{key}" if subsection else f"{section}.{key}"
            raise UntrustedCachedRepoError(f"config key {shown} is not one APM writes")


def _check_alternates(git_dir: Path, alternates_root: Path | None) -> None:
    from apm_cli.utils.realpath import resolve_path

    objects = git_dir / "objects" / "info"
    if (objects / "http-alternates").exists():
        raise UntrustedCachedRepoError("http-alternates present")
    alternates = objects / "alternates"
    if not alternates.exists() and not alternates.is_symlink():
        return
    if alternates_root is None:
        raise UntrustedCachedRepoError("object alternates present")
    root = resolve_path(alternates_root)
    # Git reads each entry up to the newline exactly as written (no trimming).
    for line in _regular_text(alternates).split("\n"):
        if not line or line.startswith("#"):
            continue
        if line.startswith('"'):
            raise UntrustedCachedRepoError("quoted object alternate")
        target = resolve_path(git_dir / "objects" / line)
        if target != root and root not in target.parents:
            raise UntrustedCachedRepoError("object alternates leave the cache")
        # Git follows alternates recursively; APM's bares never have their own.
        for chained in ("alternates", "http-alternates"):
            if (target / "info" / chained).exists():
                raise UntrustedCachedRepoError("object alternates are chained")


def _raise(error: OSError) -> None:
    # Git's own background maintenance may remove a lock or temp entry while
    # it is being listed; an entry that no longer exists cannot be used.
    if not isinstance(error, FileNotFoundError):
        raise error


def _check_plain_tree(git_dir: Path) -> None:
    """Refuse any link or special file anywhere in the Git directory, objects included."""
    for directory, subdirs, names in os.walk(git_dir, onerror=_raise):
        for name in (*subdirs, *names):
            try:
                mode = os.lstat(os.path.join(directory, name)).st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise UntrustedCachedRepoError(f"{name} is not a plain file or directory")
    # Without these Git does not take the directory as a repository and
    # searches the enclosing directories instead.
    for name in ("objects", "refs"):
        if not (git_dir / name).is_dir():
            raise UntrustedCachedRepoError(f"{name} is missing")


def _check_hooks(git_dir: Path) -> None:
    hooks = git_dir / "hooks"
    if not hooks.exists():
        return
    if hooks.is_symlink() or not hooks.is_dir():
        raise UntrustedCachedRepoError("hooks is not a directory")
    for entry in hooks.iterdir():
        if not entry.name.endswith(".sample"):
            raise UntrustedCachedRepoError("active hook present")


def is_ref_name(name: str) -> bool:
    """Whether *name* is a ref Git could hold under ``refs/`` (never a path out of it)."""
    return _REF_NAME.fullmatch(name) is not None


def require_trusted_cached_repo(
    repo: Path,
    *,
    bare: bool,
    origin_ok: OriginCheck,
    alternates_root: Path | None = None,
) -> None:
    """Raise :class:`UntrustedCachedRepoError` unless *repo* holds only APM-written Git state.

    *origin_ok* decides whether ``remote.origin.url`` names the repository
    this cache entry is keyed by; *alternates_root* is the only directory
    object alternates may point into (``None``: no alternates allowed).
    Nothing is executed; the config is parsed in-process.
    """
    git_dir = repo if bare else repo / ".git"
    try:
        info = os.lstat(git_dir)
        if not stat.S_ISDIR(info.st_mode) or (not bare and os.path.islink(repo)):
            raise UntrustedCachedRepoError("git directory is not a plain directory")
        _check_plain_tree(git_dir)
        for name in ("commondir", "info/attributes"):
            if (git_dir / name).exists():
                raise UntrustedCachedRepoError(f"{name} present")
        # Without a valid HEAD Git does not take this as a repository and
        # searches the enclosing directories instead.
        if _HEAD.fullmatch(_regular_text(git_dir / "HEAD")) is None:
            raise UntrustedCachedRepoError("HEAD is not a ref or an object id")
        _check_config(git_dir / "config", origin_ok=origin_ok, worktree=False)
        worktree_config = git_dir / "config.worktree"
        if worktree_config.exists() or worktree_config.is_symlink():
            _check_config(worktree_config, origin_ok=origin_ok, worktree=True)
        _check_alternates(git_dir, alternates_root)
        _check_hooks(git_dir)
    except UntrustedCachedRepoError:
        raise
    except (OSError, UnicodeError) as error:
        raise UntrustedCachedRepoError(f"unreadable git state: {error}") from error


def cached_repo_is_trusted(
    repo: Path,
    *,
    bare: bool,
    origin_ok: OriginCheck,
    alternates_root: Path | None = None,
) -> bool:
    """Whether :func:`require_trusted_cached_repo` accepts *repo*."""
    try:
        require_trusted_cached_repo(
            repo, bare=bare, origin_ok=origin_ok, alternates_root=alternates_root
        )
    except UntrustedCachedRepoError as error:
        from ..utils.git_env import redact_git_diagnostic

        reason = redact_git_diagnostic(str(error))
        _log.warning("[!] Discarding cached git repository %s: %s", repo.name, reason)
        return False
    return True


def validate_cached_links(repo: Path, paths: list[str] | None, env: dict[str, str]) -> None:
    """Validate the tracked links of a cached checkout, running Git inertly."""
    from .checkout_verify import inprocess_verified

    if inprocess_verified(repo, use="links"):
        return  # Written in-process, which refuses every link (in the cone, for a sparse landing).
    from ..utils.git_sparse import validate_materialized_symlinks

    validate_materialized_symlinks(
        "git", repo, paths, env=cached_repo_env(env, repo), extra_git_args=inert_git_args()
    )


def inert_git_args() -> list[str]:
    """``-c`` args for every Git run in a persisted repository: nothing executes."""
    from ..utils.git_env import git_no_hooks_args

    return [*git_no_hooks_args(), "-c", "core.fsmonitor=false"]


def cached_repo_env(
    env: dict[str, str], repo: Path | None = None, *, offline: bool = False
) -> dict[str, str]:
    """Return *env* fenced for a persisted repository: no prompts, fixed askpass, safe transports.

    With *repo*, Git never searches above it for a repository either.
    *offline* allows no transport at all and no lazy object fetch, for runs
    that must read only what is already on disk.
    """
    fenced = dict(env)
    if repo is not None:
        fenced["GIT_CEILING_DIRECTORIES"] = os.path.realpath(Path(repo).parent)
    fenced["GIT_TERMINAL_PROMPT"] = "0"
    if fenced.get("GIT_ASKPASS") not in {"", "echo"}:
        fenced["GIT_ASKPASS"] = "echo"
    fenced["GIT_ALLOW_PROTOCOL"] = "none" if offline else _ALLOWED_PROTOCOLS
    if offline:
        fenced["GIT_NO_LAZY_FETCH"] = "1"
    return fenced
