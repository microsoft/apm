"""In-process anonymous ref listing: byte parity with git ls-remote, else fall back."""

from __future__ import annotations

import threading
import time

import pytest
import urllib3

from apm_cli.deps import anonymous_ref_listing as arl

pytestmark = [
    pytest.mark.unit,
    pytest.mark.promise("P-OWN-git-transport-selection", "P-OWN-ls-remote-tag-commits"),
]

# Patched by dotted path: the request owner is a collaborator, not the module under test.
_REQUEST = "apm_cli.utils.http_credentials.origin_bounded_request"

_A = "a" * 40
_B = "b" * 40
_C = "c" * 40
_ZERO = "0" * 40


def _pkt(text: str) -> bytes:
    raw = text.encode("ascii") if isinstance(text, str) else text
    return f"{len(raw) + 4:04x}".encode("ascii") + raw


def _advertisement(*lines: str, first_caps: str = "multi_ack side-band-64k") -> bytes:
    body = _pkt("# service=git-upload-pack\n") + b"0000"
    for index, line in enumerate(lines):
        raw = line.encode("ascii")
        if index == 0:
            raw = raw + b"\0" + first_caps.encode("ascii")
        body += _pkt(raw + b"\n")
    return body + b"0000"


_REFS = (
    f"{_A} HEAD",
    f"{_A} refs/heads/main",
    f"{_B} refs/pull/1/head",
    f"{_C} refs/tags/v1.0.0",
    f"{_A} refs/tags/v1.0.0^{{}}",
    f"{_B} refs/tags/v2.0.0",
)


def test_tags_and_heads_match_git_ls_remote_output():
    out = arl.parse_advertisement(_advertisement(*_REFS), ("refs/tags/", "refs/heads/"))

    assert out == (
        f"{_A}\trefs/heads/main\n"
        f"{_C}\trefs/tags/v1.0.0\n"
        f"{_A}\trefs/tags/v1.0.0^{{}}\n"
        f"{_B}\trefs/tags/v2.0.0\n"
    )


def test_tags_only_excludes_heads_head_and_pull_refs():
    out = arl.parse_advertisement(_advertisement(*_REFS), ("refs/tags/",))

    assert out == (f"{_C}\trefs/tags/v1.0.0\n{_A}\trefs/tags/v1.0.0^{{}}\n{_B}\trefs/tags/v2.0.0\n")


def test_empty_repository_lists_nothing():
    data = _advertisement(f"{_ZERO} capabilities^{{}}")

    assert arl.parse_advertisement(data, ("refs/tags/", "refs/heads/")) == ""


@pytest.mark.parametrize(
    "line",
    [
        f"{_A} refs/tags/v1\tinjected",
        f"{_A} refs/tags/a..b",
        f"{_A} refs/tags/.hidden",
        f"{_A} refs/tags/x.lock",
        f"{_A} refs/tags/x@{{1}}",
        f"{_A} refs/tags/a:b",
        f"{_A} refs/tags/a\\b",
        f"{_A} refs/tags/a b",
        f"{'g' * 40} refs/tags/v1",
        f"{_A[:39]} refs/tags/v1",
        f"{_A.upper()} refs/tags/v1",
        f"{_A} refs/tags/v2^{{}}",
    ],
    ids=[
        "tab",
        "dotdot",
        "leading-dot",
        "lock",
        "at-brace",
        "colon",
        "backslash",
        "space",
        "non-hex",
        "short-sha",
        "upper-sha",
        "orphan-peeled",
    ],
)
def test_untrusted_advertisement_lines_fall_back(line):
    data = _advertisement(f"{_A} refs/heads/main", line)

    with pytest.raises(arl._Fallback):
        arl.parse_advertisement(data, ("refs/tags/", "refs/heads/"))


def test_a_newline_inside_a_ref_line_falls_back():
    data = _pkt("# service=git-upload-pack\n") + b"0000"
    data += _pkt(f"{_A} refs/heads/main\0caps\n")
    data += _pkt(f"{_A} refs/tags/v1\n{_B} refs/tags/evil\n")

    with pytest.raises(arl._Fallback):
        arl.parse_advertisement(data, ("refs/tags/",))


def test_missing_service_header_or_bad_length_falls_back():
    with pytest.raises(arl._Fallback):
        arl.parse_advertisement(_pkt(f"{_A} refs/tags/v1\0caps\n"), ("refs/tags/",))
    with pytest.raises(arl._Fallback):
        arl.parse_advertisement(b"zzzz", ("refs/tags/",))


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/owner/%2e%2e",
        "https://github.com/owner/repo?x=1",
        "https://github.com/owner/repo#frag",
        "https://user@github.com/owner/repo",
        "https://github.com:443/owner/repo",
        "https://github.com/owner/../repo",
        "https://github.com/owner/.repo",
        "https://github.com/own\u0435r/repo",
        "https://github.com/owner/repo/extra",
        "http://github.com/owner/repo",
        "https://api.github.com/owner/repo",
    ],
)
def test_unusual_urls_fall_back(url):
    with pytest.raises(arl._Fallback):
        arl._repository_path(url)


def test_plain_urls_keep_owner_and_repo():
    assert arl._repository_path("https://github.com/Owner-1/repo.name_x") == (
        "/Owner-1/repo.name_x"
    )


@pytest.mark.parametrize(
    "name",
    [
        "ALL_PROXY",
        "GIT_SSL_NO_VERIFY",
        "GIT_SSL_CAPATH",
        "SSL_CERT_DIR",
        "GIT_CONFIG_PARAMETERS",
        "GIT_PROXY_COMMAND",
        "GIT_SSL_CERT",
        "GIT_SSL_KEY",
    ],
)
def test_env_that_changes_gits_request_falls_back(name):
    assert (
        arl.list_refs_anonymously(
            "https://github.com/owner/repo", include_heads=True, env={name: "x"}
        )
        is None
    )


def test_proxy_follows_libcurl_precedence_and_no_proxy():
    assert arl._proxy({"https_proxy": "http://p:1", "HTTPS_PROXY": "http://p:1"}) == "http://p:1"
    assert arl._proxy({"HTTPS_PROXY": "http://q:2"}) == "http://q:2"
    assert arl._proxy({"HTTP_PROXY": "http://q:2"}) is None
    assert arl._proxy({"https_proxy": "", "HTTPS_PROXY": "http://q:2"}) == "http://q:2"
    no_proxy_upper = {"HTTPS_PROXY": "http://q:2", "no_proxy": "", "NO_PROXY": "github.com"}
    assert arl._proxy(no_proxy_upper) is None
    assert arl._proxy({"HTTPS_PROXY": "http://q:2", "NO_PROXY": ".github.com"}) is None
    assert arl._proxy({"HTTPS_PROXY": "http://q:2", "no_proxy": "*"}) is None
    with pytest.raises(arl._Fallback):
        arl._proxy({"HTTPS_PROXY": "socks5://q:2"})
    with pytest.raises(arl._Fallback):
        arl._proxy({"HTTPS_PROXY": "https://q:2"})
    for listed in ("localhost,*", "localhost github.com", "github.com."):
        with pytest.raises(arl._Fallback):
            arl._proxy({"HTTPS_PROXY": "http://q:2", "NO_PROXY": listed})
    with pytest.raises(arl._Fallback):
        arl._proxy({"https_proxy": "http://p:1", "HTTPS_PROXY": "http://q:2"})
    with pytest.raises(arl._Fallback):
        arl._proxy({"HTTPS_PROXY": "http://q:2", "no_proxy": "a.com", "NO_PROXY": "b.com"})
    with pytest.raises(arl._Fallback):
        arl._proxy({"HTTPS_PROXY": "http://user:pw@q:2"})


def _existing(monkeypatch, *paths: str) -> None:
    present = set(paths)
    monkeypatch.setattr(arl.os.path, "isfile", lambda path: path in present)
    monkeypatch.setattr(arl.os.path, "isdir", lambda _path: False)


def _verify_paths(cafile: str | None):
    return arl.ssl.DefaultVerifyPaths(cafile, None, "", "", "", "")


def test_ca_follows_git_precedence(monkeypatch):
    import certifi

    _existing(monkeypatch, "/git.pem", "/cfg.pem", "/ssl.pem")
    monkeypatch.setattr(arl.ssl, "get_default_verify_paths", lambda: _verify_paths(None))
    config = {"http.sslcainfo": "/cfg.pem"}
    env = {"GIT_SSL_CAINFO": "/git.pem", "SSL_CERT_FILE": "/ssl.pem"}

    assert arl._verify(env, config) == "/git.pem"
    assert arl._verify({"SSL_CERT_FILE": "/ssl.pem"}, config) == "/cfg.pem"
    assert arl._verify({"SSL_CERT_FILE": "/ssl.pem"}, {}) == "/ssl.pem"
    assert arl._verify({}, {}) == certifi.where()
    with pytest.raises(arl._Fallback):
        arl._verify({}, {"http.sslbackend": "schannel"})
    with pytest.raises(arl._Fallback):
        arl._verify({"GIT_SSL_CAINFO": "/missing.pem"}, {})


def test_default_ca_is_the_platform_openssl_store(monkeypatch):
    _existing(monkeypatch, "/store.pem")
    monkeypatch.setattr(arl.ssl, "get_default_verify_paths", lambda: _verify_paths("/store.pem"))

    assert arl._verify({}, {}) == "/store.pem"


def _entries(*pairs: tuple[str, str]):
    from apm_cli.utils.git_env import GitConfigEntry

    return [GitConfigEntry("command", key, value) for key, value in pairs]


def test_config_that_cannot_touch_an_https_fetch_stays_eligible():
    entries = _entries(
        ("protocol.file.allow", "always"),
        ("protocol.version", "2"),
        ("credential.helper", "osxkeychain"),
        ("credential.helper", ""),
        ("http.extraheader", ""),
        ("http.https://github.com/owner/repo.extraheader", ""),
        ("credential.https://github.com.helper", ""),
        ("http.sslbackend", "openssl"),
        ("http.sslcainfo", "/ca.pem"),
    )

    assert arl._classify(entries) == {"http.sslbackend": "openssl", "http.sslcainfo": "/ca.pem"}


@pytest.mark.parametrize(
    "pair",
    [
        ("url.https://mirror/.insteadof", "https://github.com/"),
        ("http.proxy", "http://p:1"),
        ("http.sslverify", "false"),
        ("protocol.https.allow", "never"),
        ("protocol.allow", "never"),
        ("core.gitproxy", "cmd"),
        ("credential.helper", "store"),
        ("http.extraheader", "X-Token: 1"),
        ("http.https://github.com/.extraheader", "X-Token: 1"),
        ("credential.https://github.com.helper", "store"),
        ("http.sslcapath", "/certs"),
        ("http.sslcert", "/client.pem"),
        ("http.sslkey", "/client.key"),
        ("http.https://github.com/.sslcainfo", "/ca.pem"),
    ],
)
def test_config_that_changes_gits_request_falls_back(pair):
    assert arl._classify(_entries(("credential.helper", ""), pair)) is None


@pytest.mark.parametrize("allowed", ["file", "file:ssh", ""])
def test_https_not_allowed_in_gits_child_env_falls_back(allowed, monkeypatch):
    monkeypatch.setitem(arl._config_cache, (), ({}, {"GIT_ALLOW_PROTOCOL": allowed}))
    monkeypatch.setattr(_REQUEST, lambda *_a, **_k: pytest.fail("no request expected"))

    listed = arl.list_refs_anonymously("https://github.com/owner/repo", include_heads=False, env={})

    assert listed is None


@pytest.mark.parametrize(
    "data",
    [
        _advertisement(f"{_A} refs/tags/v1")[:-4],
        _pkt("# service=git-upload-pack\n") + b"0000",
        _pkt("# service=git-upload-pack\n") + b"0000" + b"0000",
        _advertisement(f"{_A} refs/tags/v1") + _pkt(f"{_B} refs/tags/v2\n"),
        _pkt("# service=git-upload-pack\n") + _pkt(f"{_A} refs/tags/v1\0caps\n") + b"0000",
    ],
    ids=["no-terminal-flush", "header-only", "no-refs", "data-after-end", "no-separator"],
)
def test_incomplete_or_overlong_framing_falls_back(data):
    with pytest.raises(arl._Fallback):
        arl.parse_advertisement(data, ("refs/tags/",))


@pytest.mark.parametrize(
    ("pairs", "eligible"),
    [
        (
            (
                ("http.https://github.com/.extraheader", "X-A: 1"),
                ("http.https://*.example.com/.extraheader", ""),
            ),
            False,
        ),
        (
            (
                ("http.https://github.com/owner/repo.extraheader", "X-A: 1"),
                ("http.https://github.com/.extraheader", ""),
            ),
            False,
        ),
        (
            (
                ("http.https://github.com/.extraheader", "X-A: 1"),
                ("http.https://github.com/.extraheader", ""),
            ),
            True,
        ),
        (
            (
                ("http.https://github.com/Owner/.extraheader", "X-A: 1"),
                ("http.https://github.com/owner/.extraheader", ""),
            ),
            False,
        ),
    ],
    ids=["other-scope-reset", "less-specific-reset", "same-scope-reset", "case-differing-scope"],
)
def test_helper_and_header_resets_apply_within_one_url_scope(pairs, eligible):
    assert (arl._classify(_entries(*pairs)) is not None) is eligible


class _TrickleRaw:
    """A body that sends one byte per read, each read taking 9 s of the clock."""

    def __init__(self, clock: list[float]) -> None:
        self.clock = clock
        self.reads = 0

    def read1(self, _amt: int, decode_content: bool) -> bytes:
        self.reads += 1
        self.clock[0] += 9.0
        return b"0"


def test_a_trickling_body_falls_back_at_the_deadline(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(arl.time, "monotonic", lambda: clock[0])
    raw = _TrickleRaw(clock)
    response = type("Response", (), {"headers": {}, "raw": raw})()

    with pytest.raises(arl._Fallback, match="time budget"):
        arl._read_body(response, deadline=arl._DEADLINE_SECONDS)

    assert raw.reads == 4
    assert clock[0] <= arl._DEADLINE_SECONDS + 10.0


def test_an_encoded_body_falls_back():
    response = type("Response", (), {"headers": {"Content-Encoding": "gzip"}, "raw": None})()

    with pytest.raises(arl._Fallback):
        arl._read_body(response, deadline=1e9)


def test_a_body_without_single_reads_falls_back():
    response = type("Response", (), {"headers": {}, "raw": object()})()

    with pytest.raises(arl._Fallback):
        arl._read_body(response, deadline=1e9)


def _eligible(monkeypatch) -> str:
    """Seed the env-keyed config verdict for ``env={}``: eligible, trusting certifi."""
    import certifi

    monkeypatch.setitem(arl._config_cache, (), ({}, {"GIT_SSL_CAINFO": certifi.where()}))
    return certifi.where()


def test_a_fetch_stuck_past_the_budget_falls_back(monkeypatch):
    ca = _eligible(monkeypatch)
    release = threading.Event()
    monkeypatch.setattr(_REQUEST, lambda *_a, **_k: release.wait(5))
    started = time.monotonic()
    try:
        with pytest.raises(arl._Fallback, match="time budget"):
            arl._fetch_within_deadline("https://github.com/o/r/info/refs", None, ca, budget=0.05)
    finally:
        release.set()

    assert time.monotonic() - started < 2.0


class _FailingRaw:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def read1(self, _amt: int, decode_content: bool) -> bytes:
        raise self.error


class _Response:
    status_code = 200

    def __init__(self, raw) -> None:
        self.raw = raw
        self.headers = {"Content-Type": "application/x-git-upload-pack-advertisement"}

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None


@pytest.mark.parametrize(
    "error",
    [
        urllib3.exceptions.ReadTimeoutError(None, "/info/refs", "read timed out"),
        urllib3.exceptions.ProtocolError("Connection broken: IncompleteRead"),
    ],
    ids=["body-timeout", "truncated-transfer"],
)
def test_transport_errors_while_reading_fall_back(monkeypatch, error):
    _eligible(monkeypatch)
    monkeypatch.setattr(_REQUEST, lambda *_a, **_k: _Response(_FailingRaw(error)))

    listed = arl.list_refs_anonymously("https://github.com/owner/repo", include_heads=True, env={})

    assert listed is None


def test_trust_context_holds_only_gits_ca():
    import certifi

    from apm_cli.core import tls_trust

    ca = certifi.where()
    with open(ca, encoding="ascii") as bundle:
        in_bundle = bundle.read().count("-----BEGIN CERTIFICATE-----")

    context = arl._git_trust_context(ca)

    assert type(context) is tls_trust._STDLIB_SSL_CONTEXT
    assert context.verify_mode == arl.ssl.CERT_REQUIRED
    assert context.check_hostname is True
    # Exactly the bundle git would use: no OS-store or APM_EXTRA_CA_BUNDLE roots.
    assert len(context.get_ca_certs()) == in_bundle


def test_proxied_tls_uses_the_same_git_only_context():
    import certifi

    adapter = arl._session_for_refs(certifi.where()).get_adapter("https://github.com/")
    direct = adapter.poolmanager.connection_pool_kw["ssl_context"]

    proxied = adapter.proxy_manager_for("http://proxy.invalid:3128")

    assert proxied.connection_pool_kw["ssl_context"] is direct


class _WorkerAbort(BaseException):
    """Not an Exception: ends the worker without a result."""


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_worker_ending_without_a_result_falls_back(monkeypatch):
    ca = _eligible(monkeypatch)

    def abort(*_args, **_kwargs):
        raise _WorkerAbort

    monkeypatch.setattr(_REQUEST, abort)

    with pytest.raises(arl._Fallback, match="without a result"):
        arl._fetch_within_deadline("https://github.com/o/r/info/refs", None, ca)


def test_trust_context_excludes_apm_additive_and_injected_roots(tmp_path, monkeypatch):
    """Red if the context were built from the context class APM's injection publishes."""
    import sys

    import certifi
    import urllib3.util.ssl_ as urllib3_ssl

    from apm_cli.core import tls_trust

    bundle = certifi.contents()
    first = bundle[: bundle.index("-----END CERTIFICATE-----") + len("-----END CERTIFICATE-----")]
    git_ca = tmp_path / "git-ca.pem"
    git_ca.write_text(first + "\n", encoding="ascii")
    monkeypatch.setitem(sys.modules, "truststore", None)
    recorded = (tls_trust._LAST_TLS_STATUS, tls_trust._LAST_TLS_SELECTION)
    state = tls_trust._capture_tls_publication_state()
    try:
        tls_trust.configure_tls_trust(env={"APM_EXTRA_CA_BUNDLE": certifi.where()})
        # The context urllib3 (and so requests) builds once APM's trust is installed.
        injected = urllib3_ssl.SSLContext(arl.ssl.PROTOCOL_TLS_CLIENT)
        injected.load_verify_locations(cafile=str(git_ca))

        context = arl._git_trust_context(str(git_ca))
    finally:
        tls_trust._restore_tls_publication_state(state)
        tls_trust._LAST_TLS_STATUS, tls_trust._LAST_TLS_SELECTION = recorded

    assert len(injected.get_ca_certs()) > 1
    assert type(context) is tls_trust._STDLIB_SSL_CONTEXT
    assert len(context.get_ca_certs()) == 1
