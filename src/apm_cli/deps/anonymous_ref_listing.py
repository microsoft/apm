"""Anonymous ref listing for public github.com without a ``git ls-remote`` child.

``GitReferenceResolver`` lists a public github.com repository's refs with an
anonymous ``git ls-remote`` first (``AuthResolver.uses_public_github_anonymous_first``).
That attempt costs a git process, a TLS handshake and two requests per
dependency. This module answers the same anonymous attempt in process: one
smart-HTTP v0 ``info/refs`` request over a pooled keep-alive connection,
returning byte-identical ``git ls-remote`` output.

It answers only when it can prove git would make the same anonymous request
(same URL, proxy and CA, no rewrite or extra config) and the response is a
well-formed advertisement. Every other case returns ``None`` and the caller
runs ``git ls-remote`` exactly as before, so the auth chain, failure
classification and output are unchanged. No credential is ever sent.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import urllib.parse
from collections.abc import Mapping

_log = logging.getLogger(__name__)

_GITHUB_HOST = "github.com"
_NAME = re.compile(r"[A-Za-z0-9._-]{1,100}", re.ASCII)
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}", re.ASCII)
_ADVERTISEMENT_TYPE = "application/x-git-upload-pack-advertisement"
_SERVICE_LINE = b"# service=git-upload-pack"
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_PKT_LINES = 500_000
_TIMEOUT = (15.0, 30.0)
# Git-config keys APM itself injects into the anonymous github.com env, with
# the only values under which git's request equals this module's.
_INJECTED_EMPTY = frozenset({"credential.helper", "http.extraheader"})
_CA_KEY = "http.sslcainfo"
_BACKEND_KEY = "http.sslbackend"
_FALLBACK_ENV = (
    "ALL_PROXY",
    "all_proxy",
    "GIT_SSL_NO_VERIFY",
    "GIT_SSL_CAPATH",
    "GIT_SSL_CERT",
    "GIT_SSL_KEY",
    "GIT_SSL_VERSION",
    "GIT_SSL_CIPHER_LIST",
    "GIT_CONFIG_PARAMETERS",
    "GIT_PROXY_COMMAND",
    "GIT_HTTP_LOW_SPEED_LIMIT",
    "GIT_HTTP_LOW_SPEED_TIME",
    "GIT_HTTP_USER_AGENT",
    "SSL_CERT_DIR",
)

_session_lock = threading.Lock()
_session = None
_config_cache: dict[tuple, dict[str, str] | None] = {}


class _Fallback(Exception):
    """This attempt cannot be proven equal to git's: run git instead."""


def _repository_path(url: str) -> str:
    """``/<owner>/<repo>`` of a plain https github.com URL, built from validated parts."""
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != _GITHUB_HOST
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.netloc != _GITHUB_HOST
    ):
        raise _Fallback("not a plain https github.com URL")
    parts = parsed.path.split("/")
    if len(parts) != 3 or parts[0] != "":
        raise _Fallback("not an owner/repo path")
    owner, repo = parts[1], parts[2]
    for name in (owner, repo):
        if not _NAME.fullmatch(name) or name.startswith(".") or ".." in name:
            raise _Fallback("owner or repo outside the plain name charset")
    return f"/{owner}/{repo}"


def _effective_config(env: Mapping[str, str]) -> dict[str, str]:
    """The git config visible to *env* (lower-cased keys), read once per env."""
    from apm_cli.utils.git_env import (
        GitUrlRewriteProbeError,
        _read_effective_git_config,
        git_subprocess_env,
    )

    fingerprint = tuple(
        sorted(
            (k, v)
            for k, v in env.items()
            if k.startswith("GIT_") or k in ("HOME", "XDG_CONFIG_HOME", "PREFIX")
        )
    )
    if fingerprint not in _config_cache:
        try:
            snapshot = _read_effective_git_config(git_subprocess_env(dict(env)))
        except (GitUrlRewriteProbeError, OSError, ValueError):
            _config_cache[fingerprint] = None
        else:
            values: dict[str, str] = {}
            unproven = False
            for entry in snapshot.entries:
                key = entry.key.lower()
                if key in _INJECTED_EMPTY:
                    unproven = unproven or bool(entry.value)
                elif key in (_CA_KEY, _BACKEND_KEY):
                    values[key] = entry.value
                elif key.startswith(("url.", "http.", "credential.", "protocol.")) or key in (
                    "core.gitproxy",
                    "core.askpass",
                ):
                    unproven = True
            _config_cache[fingerprint] = None if unproven else values
    config = _config_cache[fingerprint]
    if config is None:
        raise _Fallback("git config changes how git would fetch this URL")
    return config


def _bypasses_proxy(no_proxy: str) -> bool:
    """libcurl's NO_PROXY match for github.com."""
    for entry in no_proxy.split(","):
        entry = entry.strip().lower()
        if entry == "*":
            return True
        entry = entry.lstrip(".")
        if entry and (entry == _GITHUB_HOST or _GITHUB_HOST.endswith("." + entry)):
            return True
    return False


def _proxy(env: Mapping[str, str]) -> str | None:
    """The proxy libcurl would use for an https github.com URL, or ``None``."""
    proxy = env.get("https_proxy") or env.get("HTTPS_PROXY")
    if not proxy:
        return None
    no_proxy = env.get("no_proxy") or env.get("NO_PROXY") or ""
    if no_proxy and _bypasses_proxy(no_proxy):
        return None
    parsed = urllib.parse.urlsplit(proxy if "://" in proxy else f"http://{proxy}")
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
        raise _Fallback("proxy is not a plain http(s) proxy")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise _Fallback("proxy URL carries a path")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _verify(env: Mapping[str, str], config: Mapping[str, str]) -> str | bool:
    """The CA file git would trust (GIT_SSL_CAINFO > http.sslCAInfo > SSL_CERT_FILE)."""
    backend = config.get(_BACKEND_KEY, "").lower()
    if backend not in ("", "openssl"):
        raise _Fallback("non-default TLS backend")
    for path in (env.get("GIT_SSL_CAINFO"), config.get(_CA_KEY), env.get("SSL_CERT_FILE")):
        if path:
            if not os.path.isfile(path):
                raise _Fallback("CA file is not a regular file")
            return path
    return True


def _session_for_refs():
    global _session
    with _session_lock:
        if _session is None:
            import http.cookiejar

            import requests
            from requests.adapters import HTTPAdapter

            from apm_cli.version import get_version

            session = requests.Session()
            session.trust_env = False
            session.cookies.set_policy(http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))
            session.headers.clear()
            session.headers.update(
                {
                    "User-Agent": f"apm/{get_version()}",
                    "Accept": "*/*",
                    "Accept-Encoding": "gzip",
                    "Pragma": "no-cache",
                }
            )
            session.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=16))
            _session = session
        return _session


def _read_body(response) -> bytes:
    chunks = []
    size = 0
    for chunk in response.iter_content(chunk_size=65536):
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise _Fallback("advertisement exceeds the size cap")
        chunks.append(chunk)
    return b"".join(chunks)


def _valid_refname(name: str) -> bool:
    """git check-ref-format rules for an advertised ref (ASCII only)."""
    if not name.startswith("refs/") or not name.isascii():
        return False
    if any(ord(c) < 0x20 or ord(c) == 0x7F or c in " ~^:?*[\\\t\n" for c in name):
        return False
    if ".." in name or "@{" in name or name.endswith(("/", ".")) or "//" in name:
        return False
    return all(
        part and not part.startswith(".") and not part.endswith(".lock") for part in name.split("/")
    )


def parse_advertisement(data: bytes, prefixes: tuple[str, ...]) -> str:
    """``git ls-remote`` stdout for the refs under *prefixes* in a v0 advertisement."""
    pos = 0
    lines: list[str] = []
    seen_service = False
    first_ref = True
    last_tag: str | None = None
    count = 0
    while pos < len(data):
        count += 1
        if count > MAX_PKT_LINES or pos + 4 > len(data):
            raise _Fallback("malformed or oversized advertisement")
        try:
            size = int(data[pos : pos + 4], 16)
        except ValueError:
            raise _Fallback("bad pkt-line length") from None
        if size == 0:
            pos += 4
            continue
        if size < 4 or pos + size > len(data):
            raise _Fallback("bad pkt-line length")
        payload = data[pos + 4 : pos + size]
        pos += size
        if not seen_service:
            if payload.rstrip(b"\n") != _SERVICE_LINE:
                raise _Fallback("missing service header")
            seen_service = True
            continue
        if first_ref:
            payload = payload.split(b"\0", 1)[0]
            first_ref = False
        elif b"\0" in payload:
            raise _Fallback("NUL outside the first ref line")
        if payload.endswith(b"\n"):
            payload = payload[:-1]
        try:
            text = payload.decode("ascii")
        except UnicodeDecodeError:
            raise _Fallback("non-ASCII ref line") from None
        sha, sep, ref = text.partition(" ")
        if not sep or not _SHA.fullmatch(sha):
            raise _Fallback("bad object id")
        if ref == "capabilities^{}":
            continue  # empty repository: no refs
        peeled = ref.endswith("^{}")
        name = ref[:-3] if peeled else ref
        if name == "HEAD":
            last_tag = None
            continue
        if not _valid_refname(name):
            raise _Fallback("invalid refname")
        if peeled:
            if name != last_tag:
                raise _Fallback("peeled ref not after its tag")
            last_tag = None
        else:
            last_tag = name if name.startswith("refs/tags/") else None
        if name.startswith(prefixes):
            lines.append(f"{sha}\t{ref}\n")
    if not seen_service:
        raise _Fallback("empty advertisement")
    return "".join(lines)


def list_refs_anonymously(url: str, *, include_heads: bool, env: Mapping[str, str]) -> str | None:
    """``git ls-remote --tags [--heads] <url>`` stdout for an anonymous attempt, or ``None``.

    ``None`` means "run git": the caller's existing anonymous ``git ls-remote``
    then runs with *env* exactly as before.
    """
    try:
        path = _repository_path(url)
        for name in _FALLBACK_ENV:
            if env.get(name):
                raise _Fallback(f"{name} is set")
        allow = env.get("GIT_ALLOW_PROTOCOL")
        if allow is not None and "https" not in allow.split(":"):
            raise _Fallback("https is not an allowed git protocol")
        config = _effective_config(env)
        proxy = _proxy(env)
        verify = _verify(env, config)
        prefixes = ("refs/tags/", "refs/heads/") if include_heads else ("refs/tags/",)
        import requests

        session = _session_for_refs()
        try:
            with session.get(
                f"https://{_GITHUB_HOST}{path}/info/refs",
                params={"service": "git-upload-pack"},
                proxies={"https": proxy} if proxy else {},
                verify=verify,
                timeout=_TIMEOUT,
                allow_redirects=False,
                stream=True,
            ) as response:
                if response.status_code != 200:
                    raise _Fallback(f"HTTP {response.status_code}")
                content_type = response.headers.get("Content-Type", "").split(";")[0].strip()
                if content_type != _ADVERTISEMENT_TYPE:
                    raise _Fallback("not a ref advertisement")
                data = _read_body(response)
        except requests.RequestException as exc:
            raise _Fallback(type(exc).__name__) from None
        return parse_advertisement(data, prefixes)
    except _Fallback as reason:
        _log.debug("anonymous ref listing falls back to git: %s", reason)
        return None
