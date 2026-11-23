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
import ssl
import threading
import time
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
_TIMEOUT = (10.0, 10.0)
# Whole request + body, like git_remote_refs' 30 s subprocess timeout.
_DEADLINE_SECONDS = 30.0
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
_config_cache: dict[tuple, tuple[dict[str, str], dict[str, str]] | None] = {}


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


def _list_key(key: str) -> str:
    """``credential[.<url>].helper`` / ``http[.<url>].extraheader`` -> the unscoped key."""
    if key.startswith("credential.") and key.endswith(".helper"):
        return "credential.helper"
    if key.startswith("http.") and key.endswith(".extraheader"):
        return "http.extraheader"
    return key


def _classify(entries) -> dict[str, str] | None:
    """CA/backend values when *entries* cannot change git's anonymous request, else ``None``."""
    values: dict[str, str] = {}
    lists: dict[str, list[str]] = {key: [] for key in _INJECTED_EMPTY}
    for entry in entries:
        key = _list_key(entry.key.lower())
        if key in lists:
            # Multi-valued in git: an empty value resets the list. URL-scoped
            # forms (git_network_env's auth fence writes one) share the list.
            lists[key] = [*lists[key], entry.value] if entry.value else []
        elif key in (_CA_KEY, _BACKEND_KEY):
            values[key] = entry.value
        elif key.startswith(("url.", "http.", "credential.", "protocol.https.")) or key in (
            "protocol.allow",
            "core.gitproxy",
            "core.askpass",
        ):
            return None
    return None if any(lists.values()) else values


def _effective_config(env: Mapping[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """``(config, child_env)`` git's ls-remote would run with for *env*, read once per env.

    The child env comes from the same builder as ``git_remote_refs``. Any
    ``url.*`` rule falls back, so the probe URL cannot change the verdict.
    """
    from apm_cli.utils.git_env import _read_effective_git_config, git_network_env

    fingerprint = tuple(sorted(env.items()))
    if fingerprint not in _config_cache:
        try:
            child_env = git_network_env(f"https://{_GITHUB_HOST}/apm/probe", dict(env))
            config = _classify(_read_effective_git_config(child_env).entries)
        except Exception:
            # A probe failure is git's to report: the caller runs git as today.
            config, child_env = None, {}
        _config_cache[fingerprint] = None if config is None else (config, child_env)
    cached = _config_cache[fingerprint]
    if cached is None:
        raise _Fallback("git config changes how git would fetch this URL")
    return cached


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
    return _system_ca()


def _system_ca() -> str | bool:
    """The platform OpenSSL CA store git's libcurl uses; certifi only when absent."""
    paths = ssl.get_default_verify_paths()
    if paths.cafile and os.path.isfile(paths.cafile):
        return paths.cafile
    if paths.capath and os.path.isdir(paths.capath):
        return paths.capath
    return True


def _session_for_refs():
    global _session
    with _session_lock:
        if _session is None:
            import http.cookiejar

            from requests.adapters import HTTPAdapter

            from apm_cli.utils.http_credentials import CredentialScopedSession
            from apm_cli.version import get_version

            session = CredentialScopedSession(ambient_credentials=False)
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


def _read_body(response, deadline: float) -> bytes:
    chunks = []
    size = 0
    for chunk in response.iter_content(chunk_size=8192):
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise _Fallback("advertisement exceeds the size cap")
        if time.monotonic() > deadline:
            raise _Fallback("advertisement exceeds the time budget")
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
    # Framing: service header, flush, one or more ref lines, flush, end.
    phase = "service"
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
            if phase == "separator":
                phase = "refs"
            elif phase == "refs" and not first_ref:
                phase = "done"
            else:
                raise _Fallback("unexpected flush")
            continue
        if phase in ("separator", "done"):
            raise _Fallback("data outside the advertisement framing")
        if size < 4 or pos + size > len(data):
            raise _Fallback("bad pkt-line length")
        payload = data[pos + 4 : pos + size]
        pos += size
        if phase == "service":
            if payload.rstrip(b"\n") != _SERVICE_LINE:
                raise _Fallback("missing service header")
            phase = "separator"
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
    if phase != "done":
        raise _Fallback("truncated advertisement")
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
        config, child_env = _effective_config(env)
        allow = child_env.get("GIT_ALLOW_PROTOCOL")
        if allow is not None and "https" not in allow.split(":"):
            raise _Fallback("https is not an allowed git protocol")
        for name in _FALLBACK_ENV:
            if child_env.get(name):
                raise _Fallback(f"{name} is set for git")
        proxy = _proxy(child_env)
        verify = _verify(child_env, config)
        prefixes = ("refs/tags/", "refs/heads/") if include_heads else ("refs/tags/",)
        import requests

        from apm_cli.utils.http_credentials import origin_bounded_request

        refs_url = f"https://{_GITHUB_HOST}{path}/info/refs?service=git-upload-pack"
        deadline = time.monotonic() + _DEADLINE_SECONDS
        try:
            # max_hops=0: any redirect raises RedirectRefusedError and falls back.
            with origin_bounded_request(
                _session_for_refs(),
                "GET",
                refs_url,
                base_url=refs_url,
                max_hops=0,
                ambient_credentials=False,
                proxies={"https": proxy} if proxy else {},
                verify=verify,
                timeout=_TIMEOUT,
                stream=True,
            ) as response:
                if response.status_code != 200:
                    raise _Fallback(f"HTTP {response.status_code}")
                content_type = response.headers.get("Content-Type", "").split(";")[0].strip()
                if content_type != _ADVERTISEMENT_TYPE:
                    raise _Fallback("not a ref advertisement")
                data = _read_body(response, deadline)
        except (requests.RequestException, ValueError) as exc:
            raise _Fallback(type(exc).__name__) from None
        return parse_advertisement(data, prefixes)
    except _Fallback as reason:
        _log.debug("anonymous ref listing falls back to git: %s", reason)
        return None
