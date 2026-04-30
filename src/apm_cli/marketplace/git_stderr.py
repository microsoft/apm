"""Translate git stderr into actionable, ASCII-only error messages.

Callers pass captured stderr text, an optional exit code, and context
(operation name, remote).  This module classifies the failure into one
of four known modes and returns a structured ``TranslatedGitError``
with a one-line summary, an actionable hint, and the (truncated) raw
stderr.

No subprocess, network, filesystem, or logging side effects -- this is
a pure function module.

Example::

    >>> from apm_cli.marketplace.git_stderr import translate_git_stderr
    >>> err = translate_git_stderr(
    ...     "fatal: authentication failed for 'https://github.com/acme/tools'",
    ...     exit_code=128,
    ...     operation="ls-remote",
    ...     remote="acme/tools",
    ... )
    >>> err.kind.value
    'auth'

"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

_RAW_MAX_LEN = 500
_SUMMARY_MAX_LEN = 80


class GitErrorKind(Enum):
    """Known git failure modes."""

    AUTH = "auth"
    NOT_FOUND = "not_found"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TranslatedGitError:
    """Structured result of translating git stderr."""

    kind: GitErrorKind
    summary: str
    hint: str
    raw: str


# -- classification patterns (lower-cased, priority order) ------------------

_AUTH_PATTERNS: list[str] = [
    "authentication failed",
    "invalid credentials",
    "could not read password",
    "permission denied (publickey)",
    "403 forbidden",
    "401 unauthorized",
    "fatal: authentication",
    "remote: write access",
    "please make sure you have the correct access rights",
    "the requested url returned error: 401",
    "the requested url returned error: 403",
]

_NOT_FOUND_PATTERNS: list[str] = [
    "repository not found",
    "does not appear to be a git repository",
    "not a valid ref",
    "couldn't find remote ref",
    "could not resolve",
    "the requested url returned error: 404",
    "no such ref",
    "unknown ref",
]

_TIMEOUT_PATTERNS: list[str] = [
    "operation timed out",
    "connection timed out",
    "could not resolve host",
    "connection refused",
    "network is unreachable",
    "temporary failure in name resolution",
    "ssl_read: connection reset",
    "early eof",
    "rpc failed",
]


def _truncate_raw(stderr: str) -> str:
    """Keep first ``_RAW_MAX_LEN`` chars; append marker if truncated."""
    if len(stderr) <= _RAW_MAX_LEN:
        return stderr
    return stderr[:_RAW_MAX_LEN] + "... (truncated)"


def _classify(stderr_lower: str) -> GitErrorKind:
    """Return the first matching error kind (priority order).

    Priority: AUTH > NOT_FOUND > TIMEOUT > UNKNOWN.

    When a NOT_FOUND pattern is a substring of a more-specific TIMEOUT
    pattern (e.g. ``"could not resolve"`` vs ``"could not resolve host"``),
    the longer TIMEOUT match wins so that DNS failures are not
    misclassified as "not found".
    """
    for pattern in _AUTH_PATTERNS:
        if pattern in stderr_lower:
            return GitErrorKind.AUTH
    for pattern in _NOT_FOUND_PATTERNS:
        if pattern in stderr_lower:
            # "could not resolve host" is a DNS/network issue, not not-found.
            if pattern == "could not resolve" and "could not resolve host" in stderr_lower:
                continue
            return GitErrorKind.NOT_FOUND
    for pattern in _TIMEOUT_PATTERNS:
        if pattern in stderr_lower:
            return GitErrorKind.TIMEOUT
    return GitErrorKind.UNKNOWN


def _build_summary(kind: GitErrorKind, operation: str, exit_code: int | None) -> str:
    """Build a one-line ASCII summary, capped at ``_SUMMARY_MAX_LEN`` chars."""
    if kind == GitErrorKind.AUTH:
        text = f"Git authentication failed during {operation}."
    elif kind == GitErrorKind.NOT_FOUND:
        text = f"Git ref or repository not found during {operation}."
    elif kind == GitErrorKind.TIMEOUT:
        text = f"Git network timeout during {operation}."
    else:
        if exit_code is not None:
            text = f"Git failed during {operation} (exit {exit_code})."
        else:
            text = f"Git failed during {operation}."

    if len(text) > _SUMMARY_MAX_LEN:
        text = text[: _SUMMARY_MAX_LEN - 3] + "..."
    return text


def _build_hint(kind: GitErrorKind, operation: str, remote: str | None) -> str:
    """Build a one-line actionable ASCII hint."""
    if kind == GitErrorKind.AUTH:
        return (
            "Check your GITHUB_TOKEN / gh auth / SSH key. "
            "Run 'apm marketplace doctor' to diagnose."
        )
    if kind == GitErrorKind.NOT_FOUND:
        remote_label = f"'{remote}'" if remote else "the remote"
        return (
            f"Verify the remote {remote_label} exists "
            "and the ref is spelled correctly."
        )
    if kind == GitErrorKind.TIMEOUT:
        return "Network issue contacting the remote. Retry or check your connection."
    # UNKNOWN
    return f"Git failed during {operation}. See raw stderr above."


def translate_git_stderr(
    stderr: str,
    *,
    exit_code: int | None = None,
    operation: str = "git operation",
    remote: str | None = None,
) -> TranslatedGitError:
    """Classify git stderr text into a known failure mode and produce an actionable hint."""
    kind = _classify(stderr.lower())
    return TranslatedGitError(
        kind=kind,
        summary=_build_summary(kind, operation, exit_code),
        hint=_build_hint(kind, operation, remote),
        raw=_truncate_raw(stderr),
    )
