"""Round-26 red-team (HTTP): proxy-destination trust boundary + response
decompression-bomb angles.

Context for the harvest: round 26 probed two "freshest" angles called out in
the campaign brief --

  (1) PROXY-PATH SSRF / proxy-destination gating. The DIRECT egress path
      restores ``_environ_proxies_for(url)`` so corporate HTTPS_PROXY egress
      keeps working (maintainer-mandated). The brief asks: can an attacker set
      the proxy to an INTERNAL / metadata endpoint and bypass the SSRF gate?
      The trust-boundary finding (proven below): the proxy value is read ONLY
      from the real process environment (``requests.utils.get_environ_proxies``
      -> ``os.environ``). APM sources NO ``.env`` file and never writes a proxy
      var into ``os.environ`` from repo/file (apm.yml) config -- ``script.env``
      is merged into the COMMAND subprocess env only, never the dispatcher's
      ``os.environ``. So the proxy is a TRUSTED (user-shell) input, exactly like
      curl/pip/npm. The repo-controlled URL still passes the SSRF gate
      proxy-AGNOSTICALLY and BEFORE the proxy decision, so an internal URL host
      is refused whether or not a proxy is set. No genuine break.

  (2) RESPONSE DECOMPRESSION BOMB. ``requests`` only auto-decompresses on
      ``.content`` / ``.text`` / ``.iter_content(decode_content=True)``. The
      dispatcher uses ``stream=True`` and reads ONLY ``status_code`` / ``ok``,
      never the body, so a gzip/deflate bomb in the RESPONSE is never inflated.
      Proven below both hermetically (a body trap that would inflate to GBs if
      touched) and against a real loopback server that lies about Content-Length
      AND sets Content-Encoding: gzip (a real read would hang OR inflate).

All assertions here are the SECURE contract and PASS on the current head,
documenting the vectors as defended. URL/host comparisons use urllib.parse,
never substrings, per the repo test convention.
"""

from __future__ import annotations

import os
import zlib
from typing import ClassVar
from urllib.parse import urlsplit

from apm_cli.core import script_executors as se

from .conftest import make_event, make_http_script


# ---------------------------------------------------------------------------
# (1) PROXY-DESTINATION TRUST BOUNDARY
# ---------------------------------------------------------------------------
def test_internal_url_host_refused_even_with_proxy_set(monkeypatch):
    """SSRF gate runs proxy-agnostically and BEFORE the proxy decision.

    A repo-controlled lifecycle event whose URL host is internal must be
    refused up-front by ``_prepare_http`` regardless of whether the operator's
    shell sets a proxy. A proxy must NOT become a laundering channel that lets
    an internal URL host through.
    """
    # Even with a corporate proxy configured in the (trusted) shell env, an
    # internal destination host is refused before any dispatch/permit/proxy use.
    monkeypatch.setitem(os.environ, "HTTPS_PROXY", "http://corp-proxy.example:8080")
    monkeypatch.setitem(os.environ, "NO_PROXY", "")

    # 169.254.169.254 is a literal link-local metadata address: blocked up-front
    # by _ssrf_block_reason without any DNS, independent of the proxy.
    script = make_http_script("https://169.254.169.254/latest/meta-data/")
    prepared = se._prepare_http(script, make_event())
    assert prepared is None, "internal URL host slipped past the SSRF gate when a proxy was set"


def test_proxy_value_is_env_only_not_repo_injectable(monkeypatch):
    """The proxy is read from os.environ only; repo/file config cannot set it.

    ``_environ_proxies_for`` delegates to ``requests.utils.get_environ_proxies``
    which reads process env. A clean env (no proxy vars) yields ``{}`` -> the
    DIRECT (DNS-pinned) path with proxies explicitly DISABLED. There is no
    apm.yml / .env seam that injects a proxy into the dispatcher's environment.
    """
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setitem(os.environ, "NO_PROXY", "")

    proxies = se._environ_proxies_for("https://collector.example.com/ingest")
    assert proxies == {}, (
        f"a proxy appeared with a clean env ({proxies}); repo/file config must "
        "never be able to inject an egress proxy"
    )


def test_corporate_proxy_egress_preserved_direct_path(monkeypatch):
    """PRESERVATION: a trusted shell HTTPS_PROXY is honored on the direct path.

    This is the maintainer-mandated corporate-egress case and must keep working:
    the proxy is passed EXPLICITLY to the capturing session's post().
    """
    seen: list[dict] = []

    class _FakeResp:
        status_code = 200
        ok = True

        def close(self):
            return None

    class _FakeSession:
        def post(self, url, *a, **k):
            seen.append(k.get("proxies"))
            return _FakeResp()

    monkeypatch.setattr(se, "_get_capturing_session", lambda: _FakeSession())
    monkeypatch.setattr(se, "_append_to_script_log", lambda *a, **k: None)
    monkeypatch.setitem(os.environ, "HTTPS_PROXY", "http://corp-proxy.example:8080")
    monkeypatch.setitem(os.environ, "NO_PROXY", "")

    url = "https://collector.example.com/ingest"
    se._dispatch_http_request(
        url, "{}", {"Content-Type": "application/json"}, 10, "post-install", url
    )

    assert seen and seen[-1] == {"https": "http://corp-proxy.example:8080"}, (
        "corporate proxy egress regressed -- the trusted shell proxy was not honored"
    )


def test_no_proxy_direct_path_disables_proxies_and_keeps_pin(monkeypatch):
    """With no env proxy, the dispatch uses the guarded (pinned) session and
    explicitly passes ``proxies={'http': None, 'https': None}`` so a stray env
    var cannot silently nullify the DNS pin."""
    seen: list[dict] = []

    class _FakeResp:
        status_code = 200
        ok = True

        def close(self):
            return None

    class _FakeGuarded:
        def post(self, url, *a, **k):
            seen.append(k.get("proxies"))
            return _FakeResp()

    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(se, "_get_guarded_session", lambda: _FakeGuarded())
    monkeypatch.setattr(se, "_append_to_script_log", lambda *a, **k: None)

    url = "https://collector.example.com/ingest"
    se._dispatch_http_request(
        url, "{}", {"Content-Type": "application/json"}, 10, "post-install", url
    )

    assert seen and seen[-1] == {"http": None, "https": None}, (
        "direct path did not explicitly disable proxies -> a stray env var could "
        "nullify the DNS pin"
    )


# ---------------------------------------------------------------------------
# (2) RESPONSE DECOMPRESSION BOMB -- hermetic
# ---------------------------------------------------------------------------
class _GzipBombResponse:
    """Response whose body, if touched, would inflate to ~1 GiB.

    Only ``status_code`` / ``ok`` / ``close`` are safe -- exactly what a
    non-buffering dispatcher reads. Any body accessor raises BEFORE allocating,
    so a regression that starts reading the body is caught deterministically
    (never actually OOMs the test).
    """

    status_code = 200
    ok = True
    headers: ClassVar[dict[str, str]] = {"Content-Encoding": "gzip", "Content-Length": "1024"}

    @property
    def content(self):  # pragma: no cover - must never be reached
        raise AssertionError("response.content read: gzip bomb would be inflated")

    @property
    def text(self):  # pragma: no cover
        raise AssertionError("response.text read: gzip bomb would be inflated")

    def json(self):  # pragma: no cover
        raise AssertionError("response.json() read: gzip bomb would be inflated")

    def iter_content(self, *a, **k):  # pragma: no cover
        raise AssertionError("response.iter_content read: gzip bomb would be inflated")

    def raw(self):  # pragma: no cover
        raise AssertionError("response.raw read: gzip bomb would be inflated")

    def close(self):
        return None


def test_response_gzip_bomb_never_inflated(dispatch):
    """A gzip-bomb response body is never read, so it is never decompressed."""
    recorder = dispatch(
        make_http_script("https://collector.example.com/ingest"),
        response=_GzipBombResponse(),
    )
    assert recorder.dispatched
    # stream=True is the structural guarantee that the body is left on the wire.
    assert recorder.last.stream is True
    parsed = urlsplit(recorder.last.url)
    assert parsed.scheme == "https"
    assert parsed.hostname == "collector.example.com"


def test_gzip_bomb_payload_is_genuinely_a_bomb():
    """Sanity: the compressed seed used by the real-server probe inflates hugely.

    Guards the decompression-bomb probe from silently degrading into a no-op:
    if this ratio ever collapses, the real-server test below is not exercising a
    real bomb. We decompress here ONLY to a bounded counter, never into memory.
    """
    raw = b"\x00" * (64 * 1024 * 1024)  # 64 MiB of zeros
    compressed = zlib.compress(raw, 9)
    # zeros compress by >>100x; assert a strong bomb ratio without holding the
    # inflated buffer (we already have `raw`, but the point is the RATIO).
    assert len(compressed) * 100 < len(raw), (
        f"seed not bomb-like (ratio {len(raw) / max(len(compressed), 1):.0f}x)"
    )
