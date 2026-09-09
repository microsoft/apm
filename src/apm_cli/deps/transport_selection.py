"""Transport (protocol) selection for dependency clones.

Issue microsoft/apm#778. Pure decision engine: given a dependency reference,
the user's CLI/env preferences, and whether an auth token is available,
produce an ordered :class:`TransportPlan` of attempts plus a strictness flag.

The selector contains no I/O. Discovery of git ``insteadOf`` rewrites is
delegated to an injected :class:`InsteadOfResolver` so unit tests can
substitute fakes and the orchestrator can re-use a single resolver instance
across many dependency clones in one ``apm install`` run.

Strict-by-default: explicit ``ssh://``, ``https://``, and ``http://`` URLs
are honored exactly. Cross-protocol fallback is only attempted when the user
opts in via ``--allow-protocol-fallback`` or ``APM_ALLOW_PROTOCOL_FALLBACK=1``.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

# Public env vars (also recognized by CLI flag plumbing).
ENV_PROTOCOL = "APM_GIT_PROTOCOL"
ENV_ALLOW_FALLBACK = "APM_ALLOW_PROTOCOL_FALLBACK"

# Documented escape-hatch hint surfaced on strict-mode failures.
FALLBACK_HINT = (
    "To allow cross-protocol fallback (not recommended), pass "
    "--allow-protocol-fallback, set APM_ALLOW_PROTOCOL_FALLBACK=1, "
    "or run: apm config set allow-protocol-fallback true"
)
REWRITE_FALLBACK_HINT = (
    "Git URL configuration rewrites every web attempt to the same transport. "
    "Inspect matching rules with "
    "'git config --show-origin --get-regexp ^url\\..*\\.insteadOf$'."
)


class ProtocolPreference(Enum):
    """User-stated default transport for shorthand dependencies.

    ``NONE`` means the user did not state a preference; the selector then
    consults git ``insteadOf`` config to decide between SSH and HTTPS.
    """

    NONE = "none"
    SSH = "ssh"
    HTTPS = "https"

    @classmethod
    def from_str(cls, value: str | None) -> ProtocolPreference:
        if not value:
            return cls.NONE
        v = value.strip().lower()
        if v in ("ssh",):
            return cls.SSH
        if v in ("https", "http"):
            return cls.HTTPS
        return cls.NONE


@dataclass(frozen=True)
class TransportAttempt:
    """A single clone attempt in the transport plan.

    Attributes:
        scheme: ``"ssh"``, ``"https"``, ``"http"``, or the scheme of an
            exact safe URL rewrite such as ``"file"``. Drives the URL builder.
        use_token: When ``True`` the orchestrator selects the resolved
            credential environment for an authenticated HTTPS attempt. The URL
            remains credential-free.
        label: Human-readable description for log/error output.
        requested_url: Original URL Git must receive so it applies a configured
            rewrite exactly once.
        effective_url: Resolved rewrite target used for policy and diagnostics.
    """

    scheme: str
    use_token: bool
    label: str
    requested_url: str | None = None
    effective_url: str | None = None


@dataclass(frozen=True)
class TransportPlan:
    """Ordered list of attempts plus strictness policy.

    Attributes:
        attempts: Ordered list. The orchestrator iterates in order.
        strict: When ``True`` the orchestrator must stop after the first
            failed attempt and surface a clear error. When ``False`` the
            orchestrator may try the next attempt (legacy permissive path).
        fallback_hint: Optional message to include in the error when a
            strict-mode attempt fails. Surfaces the escape-hatch flag.
    """

    attempts: list[TransportAttempt]
    strict: bool
    fallback_hint: str | None = None


@runtime_checkable
class InsteadOfResolver(Protocol):
    """Discovers ``git config url.<base>.insteadOf`` rewrites.

    Implementations return the rewritten URL when a rule matches the
    candidate, otherwise ``None``. Implementations are expected to cache
    results internally so the selector can be invoked many times per
    install without re-shelling to git.
    """

    def resolve(self, candidate_url: str) -> str | None:  # pragma: no cover - Protocol
        ...

    def has_exact_rule(self, candidate_url: str) -> bool:  # pragma: no cover - Protocol
        ...


class NoOpInsteadOfResolver:
    """Test/fallback resolver that always returns ``None``.

    Used in unit tests that don't care about ``insteadOf`` and as a graceful
    degradation when ``git`` is missing.
    """

    def resolve(self, candidate_url: str) -> str | None:
        return None

    def has_exact_rule(self, candidate_url: str) -> bool:
        return False


class GitConfigInsteadOfResolver:
    """Reads all ``url.*.insteadOf`` rewrites from git config (lazy + cached).

    Implementation note: this resolver MUST run ``git config`` with the
    process's normal environment, NOT with the downloader's locked-down
    git env (which sets ``GIT_CONFIG_GLOBAL=/dev/null`` and would suppress
    the user's ``insteadOf`` rules entirely, defeating the purpose).
    """

    def __init__(self) -> None:
        self._rewrites: list[tuple] | None = None  # list of (insteadof_value, target_base)
        self._http_headers = ()
        self._lock = threading.Lock()

    def resolve(self, candidate_url: str) -> str | None:
        if self._rewrites is None:
            with self._lock:
                if self._rewrites is None:
                    self._rewrites, self._http_headers = self._load_rewrites()
        from ..utils.git_env import (
            git_url_has_authorization,
            resolve_git_url_rewrite,
            validate_resolved_git_url_rewrite,
        )

        effective = resolve_git_url_rewrite(candidate_url, self._rewrites)
        if effective is not None:
            validate_resolved_git_url_rewrite(
                candidate_url,
                effective,
                has_authorization=git_url_has_authorization(
                    effective,
                    self._http_headers,
                ),
            )
        return effective

    def has_exact_rule(self, candidate_url: str) -> bool:
        if self._rewrites is None:
            with self._lock:
                if self._rewrites is None:
                    self._rewrites, self._http_headers = self._load_rewrites()
        return any(prefix == candidate_url for _, prefix in self._rewrites)

    @staticmethod
    def _load_rewrites() -> tuple[list[tuple], tuple]:
        """Load all ``url.*.insteadof`` entries from the user's git config.

        Returns an empty list if git is missing, exits non-zero, or no
        rewrites are configured.
        """
        from ..utils.git_env import (
            GitUrlRewriteError,
            GitUrlRewriteProbeError,
            configured_git_url_policy,
        )

        try:
            rewrites, http_headers = configured_git_url_policy()
            return list(rewrites), http_headers
        except (GitUrlRewriteError, GitUrlRewriteProbeError):
            raise
        except (FileNotFoundError, OSError, ValueError):
            return [], ()


def is_fallback_allowed(cli_flag: bool = False, env: dict | None = None) -> bool:
    """Return ``True`` when the user opted into cross-protocol fallback.

    Truthy via either the CLI flag or ``APM_ALLOW_PROTOCOL_FALLBACK=1``.
    """
    if cli_flag:
        return True
    env_map = env if env is not None else os.environ
    raw = env_map.get(ENV_ALLOW_FALLBACK, "")
    return raw.strip().lower() in ("1", "true", "yes", "on")


def protocol_pref_from_env(env: dict | None = None) -> ProtocolPreference:
    """Read :class:`ProtocolPreference` from ``APM_GIT_PROTOCOL`` env."""
    env_map = env if env is not None else os.environ
    return ProtocolPreference.from_str(env_map.get(ENV_PROTOCOL))


# Internal attempt builders kept here so the selection matrix is one file.

_AUTH_HTTPS = TransportAttempt(scheme="https", use_token=True, label="authenticated HTTPS")
_PLAIN_HTTPS = TransportAttempt(scheme="https", use_token=False, label="plain HTTPS")
_HTTP = TransportAttempt(scheme="http", use_token=False, label="insecure HTTP")
_SSH = TransportAttempt(scheme="ssh", use_token=False, label="SSH")


def _dedup_attempts(attempts: list[TransportAttempt]) -> list[TransportAttempt]:
    """Deduplicate attempts while preserving order."""
    seen = set()
    unique_attempts: list[TransportAttempt] = []
    for attempt in attempts:
        key = (attempt.scheme, attempt.use_token)
        if key in seen:
            continue
        seen.add(key)
        unique_attempts.append(attempt)
    return unique_attempts


def _resolve_rewrite_candidate(
    resolver: InsteadOfResolver,
    candidate: str,
) -> tuple[str, str | None]:
    """Resolve a candidate without propagating a duplicated .git suffix."""
    rewrite = resolver.resolve(candidate)
    if not (candidate.endswith(".git") and rewrite and rewrite.endswith(".git.git")):
        return candidate, rewrite
    has_exact_rule = getattr(resolver, "has_exact_rule", None)
    if not callable(has_exact_rule) or has_exact_rule(candidate):
        return candidate, rewrite

    unsuffixed_candidate = candidate.removesuffix(".git")
    unsuffixed_rewrite = resolver.resolve(unsuffixed_candidate)
    if unsuffixed_rewrite is not None and rewrite == f"{unsuffixed_rewrite}.git":
        return unsuffixed_candidate, unsuffixed_rewrite
    return candidate, rewrite


def _rewrite_attempt(
    attempt: TransportAttempt,
    *,
    requested_url: str | None,
    effective_url: str | None,
) -> TransportAttempt:
    """Attach one configured rewrite to the initial transport attempt."""
    if requested_url is None or effective_url is None:
        return attempt
    parsed = urlsplit(effective_url)
    effective_scheme = parsed.scheme.lower() or (
        "ssh" if "@" in effective_url.split(":", 1)[0] else "file"
    )
    return TransportAttempt(
        scheme=effective_scheme,
        use_token=attempt.use_token and effective_scheme in {"http", "https"},
        label=f"Git URL rewrite ({effective_scheme})",
        requested_url=requested_url,
        effective_url=effective_url,
    )


def _rewrite_web_attempts(
    attempts: list[TransportAttempt],
    *,
    requested_url: str | None,
    effective_url: str | None,
) -> list[TransportAttempt]:
    """Apply one web-URL rewrite to every web fallback attempt."""
    return [
        (
            _rewrite_attempt(
                attempt,
                requested_url=requested_url,
                effective_url=effective_url,
            )
            if attempt.scheme in {"http", "https"}
            else attempt
        )
        for attempt in attempts
    ]


def _permissive_plan(
    initial: list[TransportAttempt],
    chained: list[TransportAttempt],
) -> TransportPlan:
    """Return a deduplicated fallback plan with rewrite-aware diagnostics."""
    attempts = _dedup_attempts(initial + chained)
    if len(attempts) == 1 and attempts[0].effective_url is not None:
        return TransportPlan(
            attempts=attempts,
            strict=True,
            fallback_hint=REWRITE_FALLBACK_HINT,
        )
    return TransportPlan(attempts=attempts, strict=False, fallback_hint=None)


class TransportSelector:
    """Pure decision engine. Maps inputs to a :class:`TransportPlan`.

    The selector itself performs no network or git calls. It delegates
    ``insteadOf`` discovery to an injected :class:`InsteadOfResolver`.

    Args:
        insteadof_resolver: Resolver instance. Defaults to
            :class:`GitConfigInsteadOfResolver` (production behavior).
            Inject :class:`NoOpInsteadOfResolver` (or a fake) in tests.
    """

    def __init__(self, insteadof_resolver: InsteadOfResolver | None = None) -> None:
        self._resolver: InsteadOfResolver = insteadof_resolver or GitConfigInsteadOfResolver()

    def select(
        self,
        dep_ref,
        cli_pref: ProtocolPreference = ProtocolPreference.NONE,
        allow_fallback: bool = False,
        has_token: bool = False,
        candidate_url: str | None = None,
    ) -> TransportPlan:
        """Compute the transport plan for ``dep_ref``.

        Args:
            dep_ref: A :class:`~apm_cli.models.dependency.reference.DependencyReference`.
            cli_pref: Default protocol preference for shorthand deps.
                Ignored when ``dep_ref.explicit_scheme`` is set.
            allow_fallback: When ``True`` cross-protocol fallback is
                permitted (legacy behavior). When ``False`` (default,
                strict) the plan contains exactly one attempt for explicit
                URLs / pinned shorthand.
            has_token: Whether an auth token is available for this dep.
                Drives whether the auth-HTTPS attempt is included.

        Returns:
            :class:`TransportPlan`.
        """
        explicit = (getattr(dep_ref, "explicit_scheme", None) or "").lower() or None
        candidate = candidate_url
        if candidate is None and explicit is None:
            builder = getattr(dep_ref, "to_github_url", None)
            candidate = (
                builder()
                if callable(builder)
                else (
                    f"https://{getattr(dep_ref, 'host', None) or 'github.com'}/"
                    f"{getattr(dep_ref, 'repo_url', '')}"
                )
            )
        if candidate is not None:
            candidate, rewrite = _resolve_rewrite_candidate(self._resolver, candidate)
        else:
            rewrite = None
        web_candidate = candidate
        web_rewrite = rewrite
        if candidate is not None and urlsplit(candidate).scheme.lower() not in {"http", "https"}:
            web_candidate = dep_ref.to_github_url()
            if not dep_ref.is_azure_devops() and not web_candidate.endswith(".git"):
                web_candidate = f"{web_candidate}.git"
            web_candidate, web_rewrite = _resolve_rewrite_candidate(
                self._resolver,
                web_candidate,
            )

        # 1. Explicit scheme on the URL wins for the initial attempt.
        #    In strict mode (default) the plan contains exactly that one attempt.
        #    With allow_fallback (escape hatch for migration), we keep the user's
        #    explicit starting protocol and then append the opposite protocol.
        if explicit in ("ssh", "https", "http"):
            if explicit == "ssh":
                initial = [_SSH]
                chained = [_AUTH_HTTPS, _PLAIN_HTTPS] if has_token else [_PLAIN_HTTPS]
            elif explicit == "https":
                initial = [_AUTH_HTTPS] if has_token else [_PLAIN_HTTPS]
                chained = [_SSH, _PLAIN_HTTPS] if has_token else [_SSH]
            else:
                # Never embed a token in http:// URLs.
                initial = [_HTTP]
                chained = [_SSH]
            initial = [
                _rewrite_attempt(
                    initial[0],
                    requested_url=candidate,
                    effective_url=rewrite,
                )
            ]
            chained = _rewrite_web_attempts(
                chained,
                requested_url=web_candidate,
                effective_url=web_rewrite,
            )

            if not allow_fallback:
                return TransportPlan(
                    attempts=initial,
                    strict=True,
                    fallback_hint=FALLBACK_HINT,
                )

            return _permissive_plan(initial, chained)

        # 2. Shorthand (no explicit scheme). Consult the CLI preference and git
        #    insteadOf rewrites to pick the initial protocol.
        if cli_pref == ProtocolPreference.SSH:
            initial = [_SSH]
            chained = [_AUTH_HTTPS, _PLAIN_HTTPS] if has_token else [_PLAIN_HTTPS]
        elif cli_pref == ProtocolPreference.HTTPS:
            initial = [_AUTH_HTTPS] if has_token else [_PLAIN_HTTPS]
            chained = [_SSH, _PLAIN_HTTPS] if has_token else [_SSH]
        else:
            # Default shorthand initial attempt: HTTPS. If allow_fallback is on,
            # append SSH (and plain HTTPS after auth) below.
            initial = [_AUTH_HTTPS] if has_token else [_PLAIN_HTTPS]
            chained = [_SSH, _PLAIN_HTTPS] if has_token else [_SSH]
        initial = [
            _rewrite_attempt(
                initial[0],
                requested_url=candidate,
                effective_url=rewrite,
            )
        ]
        chained = _rewrite_web_attempts(
            chained,
            requested_url=web_candidate,
            effective_url=web_rewrite,
        )

        if not allow_fallback:
            return TransportPlan(
                attempts=initial,
                strict=True,
                fallback_hint=FALLBACK_HINT,
            )

        # Permissive: append the chain, dedup while preserving order.
        return _permissive_plan(initial, chained)
