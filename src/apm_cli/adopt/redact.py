"""Redaction helpers for discovery output.

Discovery prints paths, names and diagnostics -- never file content and never
credential material. Every string that reaches a renderer passes through one
of these helpers.
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .model import Scope

_PLACEHOLDER_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$|^\$[A-Za-z_][A-Za-z0-9_]*$")
_PLACEHOLDER_ANY_RE = re.compile(
    r"\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*|\$\{env:[^}]*\}|<[A-Z_][A-Z0-9_]*>"
)
_SECRET_KEY_RE = re.compile(
    r"(?i)(token|secret|passw(or)?d|api[-_]?key|auth|credential|bearer|cookie|"
    r"private[-_]?key|client[-_]?secret|session)"
)
_SECRET_VALUE_RE = re.compile(
    r"^(ghp_|github_pat_|gho_|ghu_|ghs_|ghr_|sk-|AKIA|xox[abpr]-|eyJ|glpat-|-----BEGIN)"
)
_OPAQUE_TOKEN_RE = re.compile(r"^[A-Za-z0-9+/=_\-.]{20,}$")

REDACTED = "<redacted>"

# High-confidence credential shapes. Used to refuse copying a file into .apm/;
# deliberately narrower than ``looks_like_secret`` to avoid false positives on prose.
_CONTENT_CREDENTIAL_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}\b")),
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{20,}\b")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9_-]{32,}\b")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("bearer-token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{32,}=*\b")),
    ("url-credential", re.compile(r"://[^/\s:@]+:[^/\s@]{8,}@")),
)


def contains_credential(text: str) -> str | None:
    """Return the label of the first credential-shaped token in *text*, else ``None``.

    Placeholders such as ``${VAR}`` never match; only literal secret material does.
    """
    for label, pattern in _CONTENT_CREDENTIAL_RES:
        match = pattern.search(text)
        if match and "${" not in match.group(0):
            return label
    return None


def looks_like_secret(key: str | None, value: str) -> bool:
    """Return whether *value* (under *key*) should never be shown or persisted."""
    if not isinstance(value, str) or not value:
        return False
    if _PLACEHOLDER_ANY_RE.fullmatch(value.strip()):
        return False
    if key is not None and _SECRET_KEY_RE.search(key):
        return True
    if _SECRET_VALUE_RE.match(value) or value.lower().startswith("bearer "):
        return True
    if _OPAQUE_TOKEN_RE.match(value):
        classes = sum(
            1 for check in (str.isupper, str.islower, str.isdigit) if any(check(ch) for ch in value)
        )
        return classes >= 2
    return False


class Redactor:
    """Format paths and values for display without leaking sensitive data."""

    def __init__(self, project_root: Path, home: Path | None = None) -> None:
        self.project_root = project_root.resolve(strict=False)
        self.home = (home or Path.home()).resolve(strict=False)

    def path(self, path: Path | str, scope: Scope = Scope.PROJECT) -> str:
        """Return a display path: project-relative, ``~``-relative, or collapsed."""
        candidate = Path(path)
        # Normalise without following symlinks so a link's own location is shown,
        # never the file it points at.
        resolved = Path(os.path.normpath(candidate)) if candidate.is_absolute() else candidate
        if scope is Scope.PROJECT and candidate.is_absolute():
            try:
                return PurePosixPath(resolved.relative_to(self.project_root)).as_posix()
            except ValueError:
                pass
        if candidate.is_absolute():
            try:
                return "~/" + PurePosixPath(resolved.relative_to(self.home)).as_posix()
            except ValueError:
                pass
            return self._collapse_home(resolved.as_posix())
        return PurePosixPath(candidate).as_posix()

    def _collapse_home(self, text: str) -> str:
        home_posix = self.home.as_posix()
        if text.startswith(home_posix):
            return "~" + text[len(home_posix) :]
        return text

    def url(self, url: str) -> str:
        """Drop userinfo, query and fragment from *url*."""
        try:
            parts = urlsplit(url)
        except ValueError:
            return "<redacted-url>"
        if not parts.scheme or not parts.netloc:
            return "<redacted-url>"
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path, "", ""))

    def env(self, env: Mapping[str, Any] | None) -> dict[str, str]:
        """Keep placeholder values; redact literals."""
        if not env:
            return {}
        result: dict[str, str] = {}
        for key, value in env.items():
            text = str(value)
            if _PLACEHOLDER_RE.match(text.strip()) or _PLACEHOLDER_ANY_RE.fullmatch(text.strip()):
                result[str(key)] = text
            else:
                result[str(key)] = REDACTED
        return result

    headers = env

    def command(self, command: str | None) -> str:
        """Return the basename of the executable plus an ellipsis for arguments."""
        if not command:
            return ""
        try:
            argv = shlex.split(command, posix=True)
        except ValueError:
            return "<unparseable>"
        if not argv:
            return ""
        head = PurePosixPath(argv[0].replace("\\", "/")).name or argv[0]
        return f"{head} ..." if len(argv) > 1 else head

    def args(self, args: Any) -> list[str]:
        """Keep flags and short plain words; redact anything that could carry a value."""
        if not isinstance(args, list):
            return []
        shown: list[str] = []
        for item in args:
            text = str(item)
            if text.startswith("-") and "=" not in text:
                shown.append(text)
            elif looks_like_secret(None, text) or "=" in text or "://" in text:
                shown.append(REDACTED)
            else:
                shown.append(text)
        return shown

    def mcp(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        """Return a display-safe copy of one ``dependencies.mcp`` entry."""
        out: dict[str, Any] = {}
        for key, value in entry.items():
            if key in ("env", "headers") and isinstance(value, Mapping):
                out[key] = self.env(value)
            elif key == "url" and isinstance(value, str):
                out[key] = self.url(value)
            elif key == "args":
                out[key] = self.args(value)
            elif key == "command" and isinstance(value, str):
                out[key] = self.command(value)
            elif key == "cwd" and isinstance(value, str):
                out[key] = self.path(value)
            elif key == "extra" and isinstance(value, Mapping):
                out[key] = {k: REDACTED if isinstance(v, str) else v for k, v in value.items()}
            else:
                out[key] = value
        return out
