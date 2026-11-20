"""Credentials stay on their origin across redirects (#3649, #3726)."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from apm_cli.utils.http_credentials import (
    CREDENTIAL_HEADER_NAMES,
    CredentialScopedSession,
    RedirectRefusedError,
    origin_bound_refusal,
    origin_bounded_request,
)

pytestmark = pytest.mark.component


class _Recorder(ThreadingHTTPServer):
    daemon_threads = True
    location: str | None = None
    seen: list[dict[str, str]]


class _Handler(BaseHTTPRequestHandler):
    server: _Recorder

    def log_message(self, _format: str, *_args: object) -> None:
        """Keep test output silent."""

    def do_GET(self) -> None:
        self.server.seen.append({k.lower(): v for k, v in self.headers.items()})
        if self.server.location and self.path == "/start":
            self.send_response(302)
            self.send_header("Location", self.server.location)
        else:
            self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


@contextmanager
def _server(location: str | None = None) -> Iterator[_Recorder]:
    server = _Recorder(("127.0.0.1", 0), _Handler)
    server.location = location
    server.seen = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_credential_headers_are_dropped_on_cross_origin_redirect() -> None:
    with _server() as target:
        target_url = f"http://localhost:{target.server_address[1]}/landing"
        with _server(target_url) as origin:
            origin_url = f"http://127.0.0.1:{origin.server_address[1]}/start"
            with CredentialScopedSession() as session:
                session.trust_env = False
                session.get(
                    origin_url,
                    headers={"PRIVATE-TOKEN": "tok-1", "Authorization": "Bearer tok-2"},
                    timeout=10,
                )
        assert origin.seen[0]["private-token"] == "tok-1"
        assert "private-token" not in target.seen[0]
        assert "authorization" not in target.seen[0]


def test_credential_headers_are_kept_on_same_origin_redirect() -> None:
    with _server() as origin:
        port = origin.server_address[1]
        origin.location = f"http://127.0.0.1:{port}/landing"
        with CredentialScopedSession() as session:
            session.trust_env = False
            session.get(
                f"http://127.0.0.1:{port}/start",
                headers={"PRIVATE-TOKEN": "tok-1"},
                timeout=10,
            )
        assert [seen.get("private-token") for seen in origin.seen] == ["tok-1", "tok-1"]


# ---------------------------------------------------------------------------
# origin_bounded_request: the redirect follow decision (#3726)
# ---------------------------------------------------------------------------

_BASE = "https://registry.example.com/apm"


class _ScriptedResponse:
    def __init__(self, status: int, location: str | None = None) -> None:
        self.status_code = status
        self.headers = {"Location": location} if location is not None else {}
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _ScriptedSession:
    """Answer each hop from a script and record what every hop carried."""

    def __init__(self, *responses: _ScriptedResponse) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def request(self, method: str, *, url: str, **kwargs: object) -> _ScriptedResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self._responses.pop(0)


def test_same_origin_hop_resends_credentials_and_preserves_put_body() -> None:
    session = _ScriptedSession(_ScriptedResponse(307, "/apm/v1/a/b"), _ScriptedResponse(201))

    response = origin_bounded_request(
        session,
        "PUT",
        f"{_BASE}/v1/a%2Fb",
        base_url=_BASE,
        headers={"Authorization": "Bearer t"},
        data=b"body",
    )

    assert response.status_code == 201
    assert [(c["method"], c["url"], c["data"]) for c in session.calls] == [
        ("PUT", f"{_BASE}/v1/a%2Fb", b"body"),
        ("PUT", f"{_BASE}/v1/a/b", b"body"),
    ]
    assert all(c["headers"] == {"Authorization": "Bearer t"} for c in session.calls)
    assert all(c["allow_redirects"] is False for c in session.calls)


@pytest.mark.parametrize(
    ("location", "reason"),
    [
        ("https://other.example.com/apm/x", "outside-origin"),
        ("//other.example.com/apm/x", "outside-origin"),
        ("https://registry.example.com:444/apm/x", "outside-origin"),
        ("https://xn--rgistry-bva.example.com/apm/x", "outside-origin"),
        ("http://registry.example.com/apm/x", "https-downgrade"),
        ("https://user:pw@registry.example.com/apm/x", "userinfo"),
        ("https://[fe80::1%25eth0]/apm/x", "unsupported-location"),
        ("ftp://registry.example.com/apm/x", "unsupported-location"),
        ("/apmx/v1", "outside-base-path"),
        ("/apm/../other", "outside-base-path"),
        ("/apm/%2e%2e/other", "outside-base-path"),
        ("/apm/%2E%2e%2Fother", "outside-base-path"),
        ("/apm/..%2f..%2fother", "outside-base-path"),
        ("/apm/%252e%252e/other", "outside-base-path"),
        ("/apm/x/..\\..\\other", "outside-base-path"),
        ("//apm/../other", "outside-origin"),
    ],
)
def test_out_of_bound_redirect_is_refused_and_never_requested(location: str, reason: str) -> None:
    session = _ScriptedSession(_ScriptedResponse(307, location))

    with pytest.raises(RedirectRefusedError) as excinfo:
        origin_bounded_request(
            session, "GET", f"{_BASE}/v1/x", base_url=_BASE, headers={"Authorization": "t"}
        )

    assert excinfo.value.reason == reason
    assert excinfo.value.response.status_code == 307
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "location",
    [
        "/apm",
        "/apm/",
        "/apm//v1//x",
        "/apm/v1/./x",
        "/apm/v1/../v2/x",
        "https://REGISTRY.Example.COM:443/apm/x",
    ],
)
def test_in_bound_redirect_is_followed(location: str) -> None:
    session = _ScriptedSession(_ScriptedResponse(302, location), _ScriptedResponse(200))

    response = origin_bounded_request(session, "GET", f"{_BASE}/v1/x", base_url=_BASE)

    assert response.status_code == 200
    assert len(session.calls) == 2


@pytest.mark.parametrize("status", [301, 302, 303])
def test_non_preserving_redirect_is_refused_for_put(status: int) -> None:
    session = _ScriptedSession(_ScriptedResponse(status, "/apm/v1/y"))

    with pytest.raises(RedirectRefusedError) as excinfo:
        origin_bounded_request(session, "PUT", f"{_BASE}/v1/x", base_url=_BASE, data=b"b")

    assert excinfo.value.reason == "method-change"
    assert len(session.calls) == 1


def test_fourth_hop_is_refused() -> None:
    session = _ScriptedSession(*(_ScriptedResponse(307, f"/apm/{n}") for n in range(4)))

    with pytest.raises(RedirectRefusedError) as excinfo:
        origin_bounded_request(session, "GET", f"{_BASE}/v1/x", base_url=_BASE)

    assert excinfo.value.reason == "hop-limit"
    assert len(session.calls) == 4


def test_refusal_message_names_the_origin_but_not_the_target() -> None:
    session = _ScriptedSession(_ScriptedResponse(302, "https://cdn.example.net/x?sig=secret"))

    with pytest.raises(RedirectRefusedError) as excinfo:
        origin_bounded_request(session, "GET", f"{_BASE}/v1/x", base_url=_BASE)

    assert "secret" not in str(excinfo.value)
    assert str(excinfo.value).startswith("redirect from https://registry.example.com refused")


def test_cross_origin_follow_mode_strips_every_credential_header() -> None:
    session = _ScriptedSession(
        _ScriptedResponse(302, "https://cdn.example.net/blob"), _ScriptedResponse(200)
    )
    headers = {name: "secret" for name in CREDENTIAL_HEADER_NAMES} | {"Accept": "x"}

    origin_bounded_request(
        session,
        "GET",
        f"{_BASE}/v1/x",
        base_url=_BASE,
        headers=headers,
        follow_cross_origin_without_credentials=True,
    )

    assert session.calls[1]["headers"] == {"Accept": "x"}


def test_cross_origin_follow_mode_still_refuses_https_downgrade() -> None:
    session = _ScriptedSession(_ScriptedResponse(302, "http://cdn.example.net/blob"))

    with pytest.raises(RedirectRefusedError):
        origin_bounded_request(
            session,
            "GET",
            f"{_BASE}/v1/x",
            base_url=_BASE,
            follow_cross_origin_without_credentials=True,
        )


def test_request_outside_base_is_rejected_before_any_hop() -> None:
    session = _ScriptedSession()

    with pytest.raises(ValueError, match=r"outside https://registry\.example\.com"):
        origin_bounded_request(session, "GET", "https://registry.example.com/other", base_url=_BASE)

    assert session.calls == []


def test_owner_reserves_redirect_and_auth_keywords() -> None:
    with pytest.raises(TypeError):
        origin_bounded_request(
            _ScriptedSession(), "GET", _BASE, base_url=_BASE, allow_redirects=True
        )


def test_origin_bound_refusal_accepts_base_itself() -> None:
    assert origin_bound_refusal(_BASE, base_url=_BASE + "/") is None


def test_credentialed_request_ignores_ambient_netrc(tmp_path, monkeypatch) -> None:
    with _server() as origin:
        port = origin.server_address[1]
        netrc = tmp_path / "netrc"
        netrc.write_text("machine 127.0.0.1 login ambient password ambient-secret\n")
        netrc.chmod(0o600)
        monkeypatch.setenv("NETRC", str(netrc))
        url = f"http://127.0.0.1:{port}/landing"
        with requests.Session() as session:
            origin_bounded_request(
                session, "GET", url, base_url=url, headers={"Authorization": "Bearer chosen"}
            )
            session.get(url, headers={"Authorization": "Bearer chosen"}, timeout=10)
        # The owner keeps the chosen credential; a plain session lets netrc win.
        assert origin.seen[0]["authorization"] == "Bearer chosen"
        assert origin.seen[1]["authorization"].startswith("Basic ")


def test_anonymous_request_can_exclude_ambient_netrc(tmp_path, monkeypatch) -> None:
    with _server() as origin:
        port = origin.server_address[1]
        netrc = tmp_path / "netrc"
        netrc.write_text("machine 127.0.0.1 login ambient password ambient-secret\n")
        netrc.chmod(0o600)
        monkeypatch.setenv("NETRC", str(netrc))
        url = f"http://127.0.0.1:{port}/landing"
        with requests.Session() as session:
            origin_bounded_request(session, "GET", url, base_url=url, ambient_credentials=False)
            origin_bounded_request(session, "GET", url, base_url=url)
        # An auth-chain step opts out of netrc even without a credential header.
        assert "authorization" not in origin.seen[0]
        assert origin.seen[1]["authorization"].startswith("Basic ")


@pytest.mark.parametrize(
    "target",
    [
        # Encoded delimiters that survive the decode limit cannot be judged.
        f"{_BASE}/%252525252e%252525252e/other",
        # Dot segments removed before slashes collapse escape the base.
        "https://registry.example.com/other//../apm/archive",
    ],
)
def test_paths_that_escape_the_base_under_any_normalization_are_refused(target: str) -> None:
    assert origin_bound_refusal(target, base_url=_BASE) == "outside-base-path"


def test_non_ascii_hosts_are_not_matched_through_idna_mappings() -> None:
    base = "https://fa\u00df.example/apm"

    assert origin_bound_refusal("https://fass.example/apm/x", base_url=base) == "outside-origin"
    assert origin_bound_refusal("https://FA\u00df.example/apm/x", base_url=base) is None


def test_cross_origin_follow_never_applies_ambient_credentials() -> None:
    session = _ScriptedSession(
        _ScriptedResponse(302, "https://cdn.example.net/blob"), _ScriptedResponse(200)
    )

    origin_bounded_request(
        session,
        "GET",
        f"{_BASE}/v1/x",
        base_url=_BASE,
        follow_cross_origin_without_credentials=True,
    )

    assert "auth" not in session.calls[0]
    assert session.calls[1]["auth"] is not None


def test_request_body_cannot_use_anonymous_cross_origin_follow() -> None:
    session = _ScriptedSession()

    with pytest.raises(TypeError, match="request body"):
        origin_bounded_request(
            session,
            "PUT",
            f"{_BASE}/v1/x",
            base_url=_BASE,
            data=b"body",
            follow_cross_origin_without_credentials=True,
        )

    assert session.calls == []


@pytest.mark.parametrize("suffix", ["?next=1", "/child"], ids=["query", "descendant-path"])
def test_unfollowed_redirect_request_never_follows_a_same_path_redirect(suffix: str) -> None:
    from apm_cli.utils.http_credentials import unfollowed_redirect_request

    with _server() as origin:
        start = f"http://127.0.0.1:{origin.server_address[1]}/start"
        origin.location = f"{start}{suffix}"
        response = unfollowed_redirect_request(
            "GET", start, headers={"Authorization": "token t"}, timeout=10
        )

    assert response.status_code == 302
    assert len(origin.seen) == 1
