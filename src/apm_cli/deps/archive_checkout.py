"""Eligibility gate for the archive transport (``archive_transport``).

The single place that decides whether a whole-repository dependency may be
fetched as one verified archive instead of a git clone. Everything that is
not provably equivalent to the git transport's first attempt stays on git:

- only exact-host ``github.com`` over HTTPS, no port, not ``--insecure``;
- only when the auth owner (:class:`AuthResolver`) says the git transport
  would itself start anonymously, so the archive request carries no
  credential and never widens what is sent;
- only when the downloader's canonical :class:`TransportSelector` plan starts
  with plain HTTPS to the original URL (no ``insteadOf`` rewrite to SSH or
  another host), so the archive never overrides configured git routing;
- only when Git would apply no attributes from outside the repository
  (system or global attributes file), which could make a checkout differ
  from the committed blobs;
- only when registry-only mode routes source archives and enumeration
  DIRECT;
- only for a resolved full commit SHA, and never when the persistent git
  cache already holds that commit (a warm git cache stays the fast path).
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..models.dependency.host_virtual import repository_path_segments
from ..utils.github_host import default_host

if TYPE_CHECKING:
    from ..cache.git_cache import GitCache
    from ..models.apm_package import DependencyReference
    from .github_downloader import GitHubPackageDownloader

_log = logging.getLogger(__name__)
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")


def _eligible(downloader: GitHubPackageDownloader, dep_ref: DependencyReference) -> bool:
    from .archive_transport import archive_transport_enabled

    if not (archive_transport_enabled() and _source_eligible(dep_ref)):
        return False
    git_env = downloader._cache_git_env(dep_ref)
    return (
        not _git_refuses_https(git_env)
        and not _conditional_git_config(git_env)
        and _anonymous_public_https(downloader, dep_ref)
        and not any(_ambient_git_attributes(env) for env in (_user_git_env(), git_env))
    )


def _source_eligible(dep_ref: DependencyReference) -> bool:
    """A whole repository on a plain host, and source archives route DIRECT."""
    from .egress_route import EgressRoute, EgressSurface, is_refused, route_for

    if dep_ref.is_virtual or dep_ref.is_local or dep_ref.is_insecure or dep_ref.port:
        return False
    if dep_ref.is_artifactory() or dep_ref.is_azure_devops():
        return False
    return route_for(EgressSurface.SOURCE_ARCHIVE) is EgressRoute.DIRECT and not is_refused(
        EgressSurface.SOURCE_ENUMERATION
    )


def _anonymous_public_https(
    downloader: GitHubPackageDownloader, dep_ref: DependencyReference
) -> bool:
    """github.com, anonymous first, and the git plan's first attempt is unrewritten HTTPS."""
    from .host_backends import backend_for
    from .transport_selection import initial_transport_scheme

    host = dep_ref.host or default_host()
    if (
        initial_transport_scheme(dep_ref, downloader._protocol_pref) != "https"
        or backend_for(dep_ref, downloader.auth_resolver, fallback_host=host).kind != "github"
        or downloader.auth_resolver.uses_public_github_anonymous_first(
            host, port=dep_ref.port, host_type=dep_ref.host_type
        )
        is not True
    ):
        return False
    return all(
        _unrewritten_https(downloader, dep_ref, candidate)
        for candidate in _git_candidate_urls(downloader, dep_ref)
    )


def _git_candidate_urls(
    downloader: GitHubPackageDownloader, dep_ref: DependencyReference
) -> list[str]:
    """Every URL form the git transport may hand to git for *dep_ref*.

    The clone engine's exact candidate (``_build_repo_url``, as
    ``clone_engine`` passes it to the selector) and the persistent cache
    remote (``to_github_url``), each with and without ``.git``: an
    ``insteadOf`` rule may match only one form.
    """
    forms = {
        downloader._build_repo_url(dep_ref.repo_url, use_ssh=False, dep_ref=dep_ref),
        dep_ref.to_github_url(),
    }
    candidates: list[str] = []
    for url in sorted(forms):
        base = url.removesuffix(".git")
        candidates.extend(form for form in (base, f"{base}.git") if form not in candidates)
    return candidates


def _unrewritten_https(
    downloader: GitHubPackageDownloader, dep_ref: DependencyReference, candidate: str
) -> bool:
    """The anonymous plan for *candidate* starts with plain HTTPS and no rewrite."""
    plan = downloader._transport_selector.select(
        dep_ref=dep_ref,
        cli_pref=downloader._protocol_pref,
        allow_fallback=downloader._allow_fallback,
        has_token=False,
        candidate_url=candidate,
    )
    first = plan.attempts[0] if plan.attempts else None
    return bool(
        first is not None
        and first.scheme == "https"
        and not first.use_token
        and not first.effective_url
    )


_AMBIENT_ATTRIBUTES: dict[tuple[str | None, ...], bool] = {}
_AMBIENT_LOCK = threading.Lock()


def _ambient_git_attributes(env: dict[str, str]) -> bool:
    """Whether Git, run with *env*, would read a non-empty system or global attributes file.

    Asked of Git itself (``git var GIT_ATTR_SYSTEM`` / ``GIT_ATTR_GLOBAL``) once
    per environment; concurrent resolver workers wait for the first answer. An
    unknown answer (older Git, probe failure) counts as present, which keeps
    the git transport.
    """
    key = ("attributes", *sorted(_config_inputs(env)))
    with _AMBIENT_LOCK:
        if key not in _AMBIENT_ATTRIBUTES:
            _AMBIENT_ATTRIBUTES[key] = _probe_ambient_git_attributes(env)
        return _AMBIENT_ATTRIBUTES[key]


def _conditional_git_config(git_env: dict[str, str]) -> bool:
    """Whether any ``includeIf`` could change Git's config inside the clone.

    A conditional include (``gitdir:``, ``hasconfig:remote.*.url:``, ...) can
    activate an ``insteadOf`` rewrite or an attributes file only once Git runs
    in the new repository, which no repository-neutral probe sees. Any such
    directive, in the user's config or the git transport's environment, keeps
    git; so does an unknown answer.
    """
    environments = (_user_git_env(), git_env)
    with _AMBIENT_LOCK:
        for env in environments:
            key = ("includeIf", *sorted(_config_inputs(env)))
            if key not in _AMBIENT_ATTRIBUTES:
                _AMBIENT_ATTRIBUTES[key] = _probe_conditional_includes(env)
            if _AMBIENT_ATTRIBUTES[key]:
                return True
    return False


def _git_refuses_https(git_env: dict[str, str]) -> bool:
    """Whether Git's protocol policy could refuse the HTTPS fetch the archive replaces.

    ``GIT_ALLOW_PROTOCOL`` without ``https``, or ``protocol.allow`` /
    ``protocol.https.allow`` set to anything but ``always``, in the user's or
    the git transport's environment: the git path would refuse or restrict
    HTTPS, so the archive must not fetch it either. An unknown answer counts
    as a refusal.
    """
    environments = (_user_git_env(), git_env)
    for env in environments:
        allowed = env.get("GIT_ALLOW_PROTOCOL")
        if allowed is not None and "https" not in allowed.split(":"):
            return True
    with _AMBIENT_LOCK:
        for env in environments:
            key = ("protocol", *sorted(_config_inputs(env)))
            if key not in _AMBIENT_ATTRIBUTES:
                _AMBIENT_ATTRIBUTES[key] = _probe_protocol_policy(env)
            if _AMBIENT_ATTRIBUTES[key]:
                return True
    return False


def _config_inputs(env: dict[str, str]) -> list[tuple[str, str]]:
    """The environment entries that decide which Git config files and values apply."""
    return [
        (name, value)
        for name, value in env.items()
        if name.startswith("GIT_") or name in ("HOME", "XDG_CONFIG_HOME")
    ]


def _user_git_env() -> dict[str, str]:
    """The environment the user's own Git (and ``insteadOf`` discovery) reads config with."""
    from ..utils.git_env import git_subprocess_env

    return git_subprocess_env(None)


def _outside_any_repository(env: dict[str, str], neutral: str) -> dict[str, str]:
    """*env* for a probe in *neutral*: Git must not discover a repository above it."""
    return {**env, "GIT_CEILING_DIRECTORIES": str(Path(neutral).parent)}


def _neutral_git_config(env: dict[str, str], pattern: str) -> tuple[int, str]:
    """``git config --includes --null --get-regexp PATTERN`` as Git would read it in a clone.

    No scope flag: system, global, XDG and env-provided config, following
    every unconditional ``include.path``; run from a fresh empty directory
    that cannot discover a repository. Returns ``(returncode, stdout)``;
    raises ``OSError`` / ``SubprocessError`` when Git cannot run.
    """
    import tempfile

    from ..utils.git_env import get_git_executable, run_git_text

    with tempfile.TemporaryDirectory(prefix="apm-gitcfg-") as neutral:
        result = run_git_text(
            [get_git_executable(), "config", "--includes", "--null", "--get-regexp", pattern],
            env=_outside_any_repository(env, neutral),
            cwd=neutral,
            capture_output=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    return result.returncode, result.stdout or ""


def _probe_protocol_policy(env: dict[str, str]) -> bool:
    try:
        code, out = _neutral_git_config(env, r"^protocol\.(allow|https\.allow)$")
    except (OSError, subprocess.SubprocessError):
        return True
    if code == 1:
        return False  # no protocol policy: https stays allowed
    if code != 0:
        return True
    values = [record.partition("\n")[2].strip().lower() for record in out.split("\0") if record]
    return any(value != "always" for value in values)


def _probe_conditional_includes(env: dict[str, str]) -> bool:
    try:
        code, _ = _neutral_git_config(env, r"^includeif\.")
    except (OSError, subprocess.SubprocessError):
        return True
    # 1: no conditional include anywhere. 0: found. Anything else: unknown.
    return code != 1


def _probe_ambient_git_attributes(env: dict[str, str]) -> bool:
    import tempfile

    try:
        # The clone runs outside the caller's project, so its config
        # (e.g. a project-local core.attributesFile) must not answer.
        with tempfile.TemporaryDirectory(prefix="apm-gitattr-") as neutral:
            return _attribute_files_have_rules(_outside_any_repository(env, neutral), neutral)
    except OSError:
        return True


def _attribute_files_have_rules(env: dict[str, str], cwd: str) -> bool:
    from ..utils.git_env import get_git_executable, run_git_text

    for variable in ("GIT_ATTR_SYSTEM", "GIT_ATTR_GLOBAL"):
        try:
            result = run_git_text(
                [get_git_executable(), "var", variable],
                env=env,
                cwd=cwd,
                capture_output=True,
                timeout=10,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return True
        if result.returncode == 1 and not result.stderr.strip():
            continue  # this attributes source is disabled
        if result.returncode != 0:
            return True
        path = Path(result.stdout.strip()).expanduser()
        if not path.is_absolute():
            return True  # Git resolves it in the clone's context; this probe cannot
        try:
            lines = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue
        except OSError:
            return True
        if any(line.strip() and not line.lstrip().startswith("#") for line in lines.splitlines()):
            return True
    return False


def try_archive_tree(
    downloader: GitHubPackageDownloader,
    cache: GitCache,
    dep_ref: DependencyReference,
    resolved_ref: Any,
) -> Path | None:
    """Return a verified archive tree for *dep_ref*, or None to use git."""
    commit = (getattr(resolved_ref, "resolved_commit", None) or "").lower()
    if not _SHA1_RE.match(commit) or getattr(cache, "read_only", True):
        return None
    try:
        url = dep_ref.to_github_url()
        # A warm shard is the fast path: decide that before any eligibility probe.
        if cache.has_full_checkout(url, commit):
            return None
        segments = repository_path_segments(dep_ref.repo_url)
        if len(segments) != 2 or not _eligible(downloader, dep_ref):
            return None
        from ..core.auth import AuthResolver
        from .archive_transport import ArchiveUnavailable, fetch_verified_tree
        from .host_backends import backend_for

        backend = backend_for(dep_ref, downloader.auth_resolver)
        headers = {"Accept": "application/vnd.github+json"}
        headers.update(AuthResolver.github_rest_headers(None))
        try:
            landed = fetch_verified_tree(
                cache,
                url=url,
                api_base=backend.host_info.api_base,
                owner=segments[0],
                repo=segments[1],
                commit=commit,
                api_headers=headers,
            )
        except ArchiveUnavailable as exc:
            _log.debug("Archive transport skipped for %s: %s", dep_ref.repo_url, exc)
            return None
        # Same receipt the clone path writes (_persistent_cache_checkout), so a
        # stale bare-cache ref cannot resurface through L2.
        resolver = getattr(downloader, "_tiered_resolver", None)
        if (
            resolver is not None
            and dep_ref.reference
            and resolver.remotely_resolved(dep_ref, commit) is True
        ):
            cache.remember_resolved_ref(url, dep_ref.reference, commit)
        return landed
    except Exception as exc:  # never block the git transport
        _log.debug("Archive transport error for %s: %s", dep_ref.repo_url, type(exc).__name__)
        return None
