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
import subprocess
import threading
from dataclasses import dataclass, field  # noqa: F401
from enum import Enum
from typing import List, Optional, Protocol, runtime_checkable  # noqa: F401, UP035

# Public env vars (also recognized by CLI flag plumbing).
ENV_PROTOCOL = "APM_GIT_PROTOCOL"
ENV_ALLOW_FALLBACK = "APM_ALLOW_PROTOCOL_FALLBACK"

# Documented escape-hatch hint surfaced on strict-mode failures.
FALLBACK_HINT = (
    "To allow cross-protocol fallback (not recommended), pass "
    "--allow-protocol-fallback or set APM_ALLOW_PROTOCOL_FALLBACK=1."
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
        scheme: ``"ssh"``, ``"https"``, or ``"http"``. Drives the URL
            builder.
        use_token: When ``True`` the orchestrator embeds the resolved auth
            token in the HTTPS URL (auth-HTTPS). Only meaningful for
            authenticated HTTPS attempts.
        label: Human-readable description for log/error output.
    """

    scheme: str
    use_token: bool
    label: str


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


class NoOpInsteadOfResolver:
    """Test/fallback resolver that always returns ``None``.

    Used in unit tests that don't care about ``insteadOf`` and as a graceful
    degradation when ``git`` is missing.
    """

    def resolve(self, candidate_url: str) -> str | None:
        return None


class GitConfigInsteadOfResolver:
    """Reads all ``url.*.insteadOf`` rewrites from git config (lazy + cached).

    Implementation note: this resolver MUST run ``git config`` with the
    process's normal environment, NOT with the downloader's locked-down
    git env (which sets ``GIT_CONFIG_GLOBAL=/dev/null`` and would suppress
    the user's ``insteadOf`` rules entirely, defeating the purpose).
    """

    def __init__(self) -> None:
        self._rewrites: list[tuple] | None = None  # list of (insteadof_value, target_base)
        self._lock = threading.Lock()

    def resolve(self, candidate_url: str) -> str | None:
        if self._rewrites is None:
            with self._lock:
                if self._rewrites is None:
                    self._rewrites = self._load_rewrites()
        best_prefix = ""
        best_base = ""
        for insteadof_value, target_base in self._rewrites:
            if candidate_url.startswith(insteadof_value) and len(insteadof_value) > len(
                best_prefix
            ):
                best_prefix = insteadof_value
                best_base = target_base
        if best_prefix:
            return best_base + candidate_url[len(best_prefix) :]
        return None

    @staticmethod
    def _load_rewrites() -> list[tuple]:
        """Load all ``url.*.insteadof`` entries from the user's git config.

        Returns an empty list if git is missing, exits non-zero, or no
        rewrites are configured.
        """
        try:
            result = subprocess.run(
                ["git", "config", "--get-regexp", r"^url\..*\.insteadof$"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return []
        if result.returncode != 0 or not result.stdout.strip():
            return []
        rewrites: list[tuple] = []
        suffix = ".insteadof"
        for line in result.stdout.splitlines():
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            key, insteadof_value = parts
            key_lower = key.lower()
            if not (key_lower.startswith("url.") and key_lower.endswith(suffix)):
                continue
            base = key[4 : -len(suffix)]
            if base:
                rewrites.append((insteadof_value, base))
        return rewrites


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

            if not allow_fallback:
                return TransportPlan(
                    attempts=initial,
                    strict=True,
                    fallback_hint=FALLBACK_HINT,
                )

            return TransportPlan(
                attempts=_dedup_attempts(initial + chained),
                strict=False,
                fallback_hint=None,
            )

        # 2. Shorthand (no explicit scheme). Consult the CLI preference and git
        #    insteadOf rewrites to pick the initial protocol.
        prefer_ssh = cli_pref == ProtocolPreference.SSH
        prefer_https = cli_pref == ProtocolPreference.HTTPS
        if cli_pref == ProtocolPreference.NONE:
            # Build the candidate HTTPS URL from the dep and ask the resolver.
            host = getattr(dep_ref, "host", None) or "github.com"
            candidate = f"https://{host}/{getattr(dep_ref, 'repo_url', '')}"
            rewrite = self._resolver.resolve(candidate)
            if rewrite and not rewrite.lower().startswith(("https://", "http://")):
                # Resolver mapped HTTPS -> non-HTTPS form (typically git@host:..). Prefer SSH.
                prefer_ssh = True

        if prefer_ssh:
            initial = [_SSH]
            chained = [_AUTH_HTTPS, _PLAIN_HTTPS] if has_token else [_PLAIN_HTTPS]
        elif prefer_https:
            initial = [_AUTH_HTTPS] if has_token else [_PLAIN_HTTPS]
            chained = [_SSH, _PLAIN_HTTPS] if has_token else [_SSH]
        else:
            # Default shorthand initial attempt: HTTPS. If allow_fallback is on,
            # append SSH (and plain HTTPS after auth) below.
            initial = [_AUTH_HTTPS] if has_token else [_PLAIN_HTTPS]
            chained = [_SSH, _PLAIN_HTTPS] if has_token else [_SSH]

        if not allow_fallback:
            return TransportPlan(
                attempts=initial,
                strict=True,
                fallback_hint=FALLBACK_HINT,
            )

        # Permissive: append the chain, dedup while preserving order.
        return TransportPlan(
            attempts=_dedup_attempts(initial + chained),
            strict=False,
            fallback_hint=None,
        )
