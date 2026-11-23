"""In-process anonymous ref listing: byte parity with git ls-remote, else fall back."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.deps import anonymous_ref_listing as arl

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-git-transport-selection", "P-OWN-ls-remote-tag-commits"),
]

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
    assert arl._proxy({"https_proxy": "http://p:1", "HTTPS_PROXY": "http://q:2"}) == "http://p:1"
    assert arl._proxy({"HTTPS_PROXY": "http://q:2"}) == "http://q:2"
    assert arl._proxy({"HTTP_PROXY": "http://q:2"}) is None
    assert arl._proxy({"HTTPS_PROXY": "http://q:2", "NO_PROXY": ".github.com"}) is None
    assert arl._proxy({"HTTPS_PROXY": "http://q:2", "no_proxy": "*"}) is None
    with pytest.raises(arl._Fallback):
        arl._proxy({"HTTPS_PROXY": "socks5://q:2"})
    with pytest.raises(arl._Fallback):
        arl._proxy({"HTTPS_PROXY": "http://user:pw@q:2"})


def test_ca_follows_git_precedence(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(arl, "_system_ca", lambda: True)
    git_ca, config_ca, cert_file = (tmp_path / n for n in ("git.pem", "cfg.pem", "ssl.pem"))
    for path in (git_ca, config_ca, cert_file):
        path.write_text("pem", encoding="ascii")
    config = {"http.sslcainfo": str(config_ca)}
    env = {"GIT_SSL_CAINFO": str(git_ca), "SSL_CERT_FILE": str(cert_file)}

    assert arl._verify(env, config) == str(git_ca)
    assert arl._verify({"SSL_CERT_FILE": str(cert_file)}, config) == str(config_ca)
    assert arl._verify({"SSL_CERT_FILE": str(cert_file)}, {}) == str(cert_file)
    assert arl._verify({}, {}) is True
    with pytest.raises(arl._Fallback):
        arl._verify({}, {"http.sslbackend": "schannel"})
    with pytest.raises(arl._Fallback):
        arl._verify({"GIT_SSL_CAINFO": str(tmp_path / "missing.pem")}, {})


def test_default_ca_is_the_platform_openssl_store(tmp_path: Path, monkeypatch):
    store = tmp_path / "cert.pem"
    store.write_text("pem", encoding="ascii")
    paths = arl.ssl.DefaultVerifyPaths(str(store), None, "", "", "", "")
    monkeypatch.setattr(arl.ssl, "get_default_verify_paths", lambda: paths)

    assert arl._verify({}, {}) == str(store)


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
    ],
)
def test_config_that_changes_gits_request_falls_back(pair):
    assert arl._classify(_entries(("credential.helper", ""), pair)) is None


@pytest.mark.parametrize("allowed", ["file", "file:ssh", ""])
def test_https_not_allowed_in_gits_child_env_falls_back(allowed, monkeypatch):
    child_env = {"GIT_ALLOW_PROTOCOL": allowed}
    monkeypatch.setattr(arl, "_effective_config", lambda _env: ({}, child_env))
    monkeypatch.setattr(arl, "_session_for_refs", lambda: pytest.fail("no request expected"))

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
