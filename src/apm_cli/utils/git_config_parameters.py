"""Read and filter ``GIT_CONFIG_PARAMETERS`` for the URL-only probe env (#5246).

Git passes ``-c`` style config to child processes in ``GIT_CONFIG_PARAMETERS``
as shell-quoted entries: ``'key'='value'`` (or the older ``'key=value'``),
separated by whitespace. The probe that decides which server answers for a
URL needs the URL rewrites from it and nothing else: no credential helper,
no header, no token. Parsing is strict; anything uncertain is dropped.
"""

from __future__ import annotations

from urllib.parse import urlsplit

_REWRITE_SUFFIXES = (".insteadof", ".pushinsteadof")


def _read_quoted(raw: str, index: int) -> tuple[str, int] | None:
    """One shell-quoted word at *index*: ``'...'`` runs and ``\\'`` or ``\\!`` escapes."""
    parts: list[str] = []
    while index < len(raw) and raw[index] in "'\\":
        if raw[index] == "'":
            end = raw.find("'", index + 1)
            if end < 0:
                return None
            parts.append(raw[index + 1 : end])
            index = end + 1
        elif index + 1 < len(raw) and raw[index + 1] in "'!":
            parts.append(raw[index + 1])
            index += 2
        else:
            return None
    return ("".join(parts), index) if parts else None


def parse_config_parameters(raw: str) -> list[tuple[str, str | None]] | None:
    """Every ``(key, value)`` entry of *raw*, or None when any entry is not certain."""
    entries: list[tuple[str, str | None]] = []
    index = 0
    while True:
        while index < len(raw) and raw[index].isspace():
            index += 1
        if index >= len(raw):
            return entries
        word = _read_quoted(raw, index)
        if word is None:
            return None
        key, index = word
        value: str | None = None
        if index < len(raw) and raw[index] == "=":
            word = _read_quoted(raw, index + 1)
            if word is None:
                return None
            value, index = word
        elif "=" in key:
            key, value = key.split("=", 1)
        if index < len(raw) and not raw[index].isspace():
            return None
        entries.append((key, value))


def _carries_userinfo(url: str) -> bool:
    """True when *url* names a user or password; such a rewrite is never kept."""
    if "://" in url:
        try:
            return "@" in (urlsplit(url).netloc or "")
        except ValueError:
            return True
    # scp-like "user@host:path": only a plain SSH user name (no password) is kept.
    head = url.split(":", 1)[0]
    return "@" in head and ":" in head.split("@", 1)[0]


def _quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''").replace("!", "'\\!'") + "'"


def url_rewrites_only(raw: str | None) -> str | None:
    """*raw* reduced to its ``url.<base>.insteadOf`` and ``pushInsteadOf`` entries.

    Every other entry is dropped, so are rewrites whose base or value carries
    URL userinfo; an unparseable *raw* yields None (nothing is kept).
    """
    entries = parse_config_parameters(raw or "")
    if not entries:
        return None
    kept = []
    for key, value in entries:
        lowered = key.lower()
        suffix = next((s for s in _REWRITE_SUFFIXES if lowered.endswith(s)), None)
        if value is None or suffix is None or not lowered.startswith("url."):
            continue
        base = key[len("url.") : -len(suffix)]
        if not base or _carries_userinfo(base) or _carries_userinfo(value):
            continue
        kept.append(f"{_quote(key)}={_quote(value)}")
    return " ".join(kept) or None
