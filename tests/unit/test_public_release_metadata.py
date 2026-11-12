"""Exercise public metadata recovery through production installer entrypoints."""

from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

import pytest
import requests

from apm_cli.commands.self_update import get_latest_version_for_self_update
from apm_cli.utils.version_checker import (
    ReleaseMetadataError,
    _reset_version_check_auth_resolver_for_tests,
    get_latest_version_from_github,
)

pytestmark = pytest.mark.component
ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/public_release_metadata"
TOKEN = "SYNTHETIC_NOT_A_CREDENTIAL_2833"
TOKEN_NAMES = ("GITHUB_APM_PAT", "GITHUB_TOKEN", "GH_TOKEN")
PWSH = shutil.which("pwsh")
SURFACES = [
    "python-stable",
    "python-prerelease",
    pytest.param(
        "unix",
        marks=pytest.mark.skipif(sys.platform == "win32", reason="Unix shell installer"),
    ),
    pytest.param(
        "windows",
        marks=[
            pytest.mark.windows_compat,
            pytest.mark.skipif(PWSH is None, reason="PowerShell unavailable"),
        ],
    ),
]


def response(status: int = 200, body: object = None, headers: dict[str, str] | None = None) -> dict:
    """Create one synthetic release response."""
    if body is None:
        body = {
            "tag_name": "v99.0.0",
            "prerelease": True,
            "assets": [{"name": "apm-windows-x86_64.zip"}],
        }
    return {"status": status, "body": body, "headers": headers or {}}


def run_lookup(
    surface: str,
    tmp_path: Path,
    responses: list[dict],
    extra_env: dict[str, str] | None = None,
) -> tuple[bool, list[dict], str]:
    """Run real discovery, isolating credentials and blocking installation."""
    env = {
        "PATH": os.defpath,
        "HOME": str(tmp_path),
        "LOCALAPPDATA": str(tmp_path),
        "APM_INSTALL_DIR": str(tmp_path / "never-installed"),
        "APM_TEMP_DIR": str(tmp_path),
        "HTTP_RESPONSES": json.dumps(responses),
        "EXPECTED_TOKEN": TOKEN,
        "TEST_PYTHON": sys.executable,
        "TEST_FIXTURES": str(FIXTURES),
        "TEST_ROOT": str(ROOT),
        "TEST_OS": "Darwin",
        "POWERSHELL_TELEMETRY_OPTOUT": "1",
        "POWERSHELL_UPDATECHECK": "Off",
        "PSModuleAnalysisCachePath": str(tmp_path / "ps-cache"),
    }
    if sys.platform == "win32":
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    env.update(extra_env or {})
    calls = []
    if surface.startswith("python"):

        def fake_get(url: str, headers: dict, timeout: int, **kwargs: object) -> requests.Response:
            authenticated = "Authorization" in headers
            if authenticated:
                assert headers["Authorization"] == "token " + env["EXPECTED_TOKEN"]
            calls.append({"url": url, "authenticated": authenticated})
            fixture = responses[0 if authenticated else -1]
            if fixture["status"] == 0:
                raise requests.ConnectionError("Synthetic network failure")
            result = requests.Response()
            result.status_code = fixture["status"]
            result.headers.update(fixture["headers"])
            body = fixture["body"]
            if urlparse(url).query == "per_page=5" and fixture["status"] == 200:
                body = [body]
            result._content = json.dumps(body).encode()
            result.url = url
            return result

        _reset_version_check_auth_resolver_for_tests()
        try:
            with patch.dict(os.environ, env, clear=True), patch("requests.get", fake_get):
                try:
                    version = get_latest_version_for_self_update(surface.removeprefix("python-"))
                except ReleaseMetadataError as exc:
                    return False, calls, str(exc)
            return version == "99.0.0", calls, ""
        finally:
            _reset_version_check_auth_resolver_for_tests()
    command = (
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(FIXTURES / "windows.ps1")]
        if surface == "windows"
        else ["/bin/bash", str(FIXTURES / "unix.sh")]
    )
    result = subprocess.run(
        command, env=env, capture_output=True, text=True, timeout=30, check=False, cwd=tmp_path
    )
    output = result.stdout + result.stderr
    assert TOKEN not in output
    assert "Unexpected" not in output, output
    calls = [
        json.loads(line.removeprefix("REQUEST "))
        for line in result.stderr.splitlines()
        if line.startswith("REQUEST ")
    ]
    return result.returncode == 97 and "METADATA_CHECKPOINT" in output, calls, output


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("token_name", TOKEN_NAMES)
@pytest.mark.parametrize("status", [401, 403])
def test_rejected_public_token_recovers_once(
    surface: str, token_name: str, status: int, tmp_path: Path
) -> None:
    """Rejected ambient credentials must not prevent public APM discovery."""
    ok, calls, output = run_lookup(
        surface,
        tmp_path,
        [response(status, {"message": "Bad credentials"}), response()],
        {token_name: TOKEN},
    )
    assert ok, output
    assert [call["authenticated"] for call in calls] == [True, False]
    assert {urlparse(call["url"]).hostname for call in calls} == {"api.github.com"}


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("token_name", [None, *TOKEN_NAMES])
def test_accepted_or_missing_token_needs_one_request(
    surface: str, token_name: str | None, tmp_path: Path
) -> None:
    """Accepted tokens avoid anonymous shared-IP limits; missing ones are optional."""
    replies = [response(), response(403, {"message": "API rate limit exceeded"})]
    if token_name is None:
        replies = [response()]
    ok, calls, output = run_lookup(
        surface, tmp_path, replies, {token_name: TOKEN} if token_name else {}
    )
    assert ok, output
    assert [call["authenticated"] for call in calls] == [token_name is not None]


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize(
    "extra_env,expected_auth,expected_calls",
    [
        ({"APM_REPO": "private/apm"}, True, 1),
        ({"GITHUB_URL": "https://ghe.example", "GITHUB_HOST": "ghe.example"}, True, 1),
        (
            {
                "GITHUB_URL": "https://github.com.evil.example",
                "GITHUB_HOST": "github.com.evil.example",
            },
            True,
            1,
        ),
        ({"APM_RELEASE_METADATA_URL": "https://mirror.example/latest.json"}, False, 1),
        (
            {
                "APM_RELEASE_METADATA_URL": "https://api.github.com/repos/microsoft/apm/releases/latest"
            },
            False,
            1,
        ),
        ({"APM_NO_DIRECT_FALLBACK": "1"}, False, 0),
    ],
)
def test_recovery_does_not_cross_operator_boundaries(
    surface: str, extra_env: dict, expected_auth: bool, expected_calls: int, tmp_path: Path
) -> None:
    """Private/GHES/mirror/no-direct settings cannot gain public anonymous recovery."""
    ok, calls, _ = run_lookup(
        surface,
        tmp_path,
        [response(401, {"message": "Bad credentials"})],
        {"GITHUB_APM_PAT": TOKEN, **extra_env},
    )
    assert not ok
    assert [call["authenticated"] for call in calls] == [expected_auth] * expected_calls
    if calls and "APM_RELEASE_METADATA_URL" in extra_env:
        assert {urlparse(call["url"]).hostname for call in calls} == {
            urlparse(extra_env["APM_RELEASE_METADATA_URL"]).hostname
        }


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize(
    "failure",
    [
        response(403, {"message": "API rate limit exceeded"}),
        response(403, {"message": "You have exceeded a secondary rate limit."}),
        response(403, {}, {"X-RateLimit-Remaining": "0"}),
        response(403, {}, {"Retry-After": "60"}),
        response(429, {}),
        response(404, {"message": "Not Found"}),
        response(500, {}),
        response(0, {}),
        response(200, {}),
    ],
    ids=[
        "primary-body",
        "secondary-body",
        "primary-header",
        "retry-after",
        "429",
        "404",
        "500",
        "network",
        "malformed",
    ],
)
def test_other_failures_do_not_downgrade_or_repeat_credentials(
    surface: str, failure: dict, tmp_path: Path
) -> None:
    """Throttle, network, HTTP and malformed responses are not auth rejections."""
    ok, calls, output = run_lookup(surface, tmp_path, [failure], {"GITHUB_TOKEN": TOKEN})
    assert not ok
    assert [call["authenticated"] for call in calls] == [True]
    if failure["status"] in (403, 429):
        assert "rate limit" in output
    elif failure["status"] == 0:
        assert "network request failed" in output
    elif failure["status"] != 200:
        assert f"HTTP {failure['status']}" in output
    elif surface != "python-prerelease":
        assert "Invalid release metadata" in output


@pytest.mark.parametrize("surface", SURFACES)
def test_recovery_is_bounded_when_anonymous_also_rejected(surface: str, tmp_path: Path) -> None:
    """A public rejection permits exactly one anonymous attempt, never a loop."""
    ok, calls, _ = run_lookup(surface, tmp_path, [response(401, {})], {"GITHUB_TOKEN": TOKEN})
    assert not ok
    assert [call["authenticated"] for call in calls] == [True, False]


@pytest.mark.parametrize("surface", SURFACES)
def test_pinned_version_skips_metadata(surface: str, tmp_path: Path) -> None:
    """Pinned versions bypass all metadata/auth operations."""
    ok, calls, output = run_lookup(
        surface, tmp_path, [], {"VERSION": "v99.0.0", "GITHUB_TOKEN": TOKEN}
    )
    assert ok, output
    assert calls == []


@pytest.mark.parametrize("surface", ["python-stable", "python-prerelease"])
def test_org_token_resolution_stays_with_auth_resolver(surface: str, tmp_path: Path) -> None:
    """Per-org credentials retain precedence without introducing raw token reads."""
    ok, calls, _ = run_lookup(
        surface,
        tmp_path,
        [response(401, {}), response()],
        {"GITHUB_APM_PAT_MICROSOFT": TOKEN, "GITHUB_TOKEN": "SYNTHETIC_LOWER_PRIORITY"},
    )
    assert ok
    assert [call["authenticated"] for call in calls] == [True, False]


@pytest.mark.skipif(sys.platform == "win32", reason="Unix shell installer")
def test_linux_metadata_recovery(tmp_path: Path) -> None:
    """The Linux compatibility path reaches the same bounded metadata retry."""
    ok, calls, output = run_lookup(
        "unix",
        tmp_path,
        [response(401, {}), response()],
        {"TEST_OS": "Linux", "GITHUB_TOKEN": TOKEN},
    )
    assert ok, output
    assert [call["authenticated"] for call in calls] == [True, False]


@pytest.mark.parametrize("surface", SURFACES)
def test_recovery_retains_credential_precedence(surface: str, tmp_path: Path) -> None:
    """No retry cycles through lower-priority credentials."""
    ok, calls, _ = run_lookup(
        surface,
        tmp_path,
        [response(401, {}), response()],
        {
            "GITHUB_APM_PAT": TOKEN,
            "GITHUB_TOKEN": "SYNTHETIC_LOWER_PRIORITY",
            "GH_TOKEN": "SYNTHETIC_LOWEST_PRIORITY",
        },
    )
    assert ok
    assert [call["authenticated"] for call in calls] == [True, False]


@pytest.mark.parametrize("mirror", [False, True])
def test_prepared_requests_exclude_netrc_and_redirects(mirror: bool, tmp_path: Path) -> None:
    """Real requests preparation cannot substitute netrc credentials on recovery/mirrors."""
    env = {"GITHUB_TOKEN": TOKEN, "HOME": str(tmp_path)}
    if mirror:
        env["APM_RELEASE_METADATA_URL"] = "https://mirror.example/latest.json"
    sent = []

    def send(request: requests.PreparedRequest, **kwargs: object) -> requests.Response:
        sent.append(request)
        assert kwargs["allow_redirects"] is False
        result = requests.Response()
        result.status_code = 401 if request.headers.get("Authorization") else 200
        result._content = b'{"tag_name":"v99.0.0"}'
        return result

    with (
        patch.dict(os.environ, env, clear=True),
        patch("requests.sessions.get_netrc_auth", side_effect=AssertionError("netrc forbidden")),
        patch("requests.Session.send", side_effect=send),
    ):
        assert get_latest_version_for_self_update("stable") == "99.0.0"
    assert [bool(request.headers.get("Authorization")) for request in sent] == (
        [False] if mirror else [True, False]
    )
    assert {urlparse(request.url).hostname for request in sent} == (
        {"mirror.example"} if mirror else {"api.github.com"}
    )


@pytest.mark.parametrize("payload", [b"not json", b"[]", b"null", b'{"tag_name":42}'])
def test_invalid_metadata_is_not_a_network_failure(payload: bytes) -> None:
    """Explicit checks diagnose malformed JSON/shape; startup checks remain quiet."""
    result = requests.Response()
    result.status_code = 200
    result._content = payload
    with patch.dict(os.environ, {}, clear=True), patch("requests.get", return_value=result):
        with pytest.raises(ReleaseMetadataError, match="Invalid release metadata"):
            get_latest_version_for_self_update("stable")
        assert get_latest_version_from_github() is None


@pytest.mark.parametrize("surface", SURFACES)
def test_server_errors_never_echo_credentials(surface: str, tmp_path: Path) -> None:
    """Untrusted response bodies are evidence, not terminal diagnostics."""
    ok, calls, output = run_lookup(
        surface,
        tmp_path,
        [response(401, {"message": TOKEN})],
        {"GITHUB_TOKEN": TOKEN},
    )
    assert not ok
    assert TOKEN not in output
    assert [call["authenticated"] for call in calls] == [True, False]


@pytest.mark.skipif(sys.platform == "win32", reason="Unix shell installer")
@pytest.mark.parametrize("status", [200, 403])
def test_interim_headers_do_not_hide_final_metadata_status(status: int, tmp_path: Path) -> None:
    """Early Hints cannot hide a primary throttle or corrupt successful metadata."""
    final = response(status, headers={"X-RateLimit-Remaining": "0"})
    final["interim"] = True
    ok, calls, output = run_lookup("unix", tmp_path, [final], {"GITHUB_TOKEN": TOKEN})
    assert ok == (status == 200), output
    assert [call["authenticated"] for call in calls] == [True]
    if status == 403:
        assert "rate limit" in output


def test_invalid_ca_bundle_is_a_quiet_or_structured_transport_failure(tmp_path: Path) -> None:
    """Requests' pre-network OSError must not escape the startup/CLI contract."""
    env = {"REQUESTS_CA_BUNDLE": str(tmp_path / "missing-ca.pem")}
    with (
        patch.dict(os.environ, env, clear=True),
        patch("socket.socket.connect", side_effect=AssertionError("network forbidden")),
    ):
        assert get_latest_version_from_github() is None
        with pytest.raises(ReleaseMetadataError, match="network request failed"):
            get_latest_version_for_self_update("stable")


@pytest.mark.parametrize(
    "env",
    [
        {"GITHUB_TOKEN": TOKEN + "\nnot-a-header"},
        {"APM_RELEASE_METADATA_URL": "https://[invalid"},
    ],
)
def test_request_configuration_errors_are_not_json_errors(env: dict[str, str]) -> None:
    """InvalidHeader/InvalidURL also inherit ValueError, but no JSON was received."""
    with (
        patch.dict(os.environ, env, clear=True),
        patch("requests.Session.send", side_effect=AssertionError("network forbidden")),
    ):
        assert get_latest_version_from_github() is None
        with pytest.raises(ReleaseMetadataError, match="network request failed") as failure:
            get_latest_version_for_self_update("stable")
    assert TOKEN not in str(failure.value)


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("status", [200, 503])
def test_mirrors_never_receive_token_or_fall_back(
    surface: str, status: int, tmp_path: Path
) -> None:
    """Mirror metadata remains credential-free in successful and failed no-direct runs."""
    ok, calls, output = run_lookup(
        surface,
        tmp_path,
        [response(status)],
        {
            "GITHUB_TOKEN": TOKEN,
            "APM_RELEASE_METADATA_URL": "https://mirror.example/latest.json",
            "APM_RELEASE_BASE_URL": "https://mirror.example/releases",
            "APM_NO_DIRECT_FALLBACK": "1",
        },
    )
    assert ok == (status == 200), output
    assert [call["authenticated"] for call in calls] == [False]
    assert {urlparse(call["url"]).hostname for call in calls} == {"mirror.example"}


@pytest.mark.windows_compat
@pytest.mark.skipif(PWSH is None, reason="PowerShell unavailable")
@pytest.mark.parametrize("headers", [{"X-RateLimit-Remaining": "0"}, {"Retry-After": "60"}])
def test_windows_powershell_legacy_throttle_headers(
    headers: dict[str, str], tmp_path: Path
) -> None:
    """Windows PowerShell's WebHeaderCollection carries the same throttle signals."""
    ok, calls, output = run_lookup(
        "windows",
        tmp_path,
        [response(403, {}, headers)],
        {"GITHUB_TOKEN": TOKEN, "PS_HEADERS_STYLE": "legacy"},
    )
    assert not ok
    assert "rate limit" in output
    assert [call["authenticated"] for call in calls] == [True]


@pytest.mark.windows_compat
@pytest.mark.skipif(PWSH is None, reason="PowerShell unavailable")
@pytest.mark.parametrize("mirror", [False, True])
def test_windows_metadata_ignores_caller_auth_defaults(mirror: bool, tmp_path: Path) -> None:
    """Actual PowerShell binding cannot re-authenticate recovery or mirror traffic."""
    env = {"GITHUB_TOKEN": TOKEN, "PS_METADATA_DEFAULTS": "1"}
    if mirror:
        env["APM_RELEASE_METADATA_URL"] = "https://mirror.example/latest.json"
    ok, calls, output = run_lookup("windows", tmp_path, [response(401), response()], env)
    assert ok, output
    assert [call["authenticated"] for call in calls] == ([False] if mirror else [True, False])


@pytest.mark.windows_compat
@pytest.mark.skipif(PWSH is None, reason="PowerShell unavailable")
def test_windows_network_failure_survives_caller_strict_mode(tmp_path: Path) -> None:
    """An exception without an HTTP Response still produces a safe network diagnostic."""
    ok, calls, output = run_lookup(
        "windows", tmp_path, [response(0)], {"PS_STRICT_METADATA": "1", "GITHUB_TOKEN": TOKEN}
    )
    assert not ok
    assert "network request failed" in output
    assert [call["authenticated"] for call in calls] == [True]


@pytest.mark.windows_compat
@pytest.mark.skipif(PWSH is None, reason="PowerShell unavailable")
def test_windows_stable_path_reaches_path_consumer(tmp_path: Path) -> None:
    """The production promotion passes its stable executable directory to PATH unchanged."""
    install_root = tmp_path / "APM Stable Path & Edge"
    env = {
        "PATH": os.defpath,
        "HOME": str(tmp_path),
        "TEST_ROOT": str(ROOT),
        "TEST_INSTALL_ROOT": str(install_root),
        "POWERSHELL_TELEMETRY_OPTOUT": "1",
        "POWERSHELL_UPDATECHECK": "Off",
        "PSModuleAnalysisCachePath": str(tmp_path / "ps-cache"),
    }
    if sys.platform == "win32":
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    result = subprocess.run(
        [
            PWSH,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(FIXTURES / "stable-path.ps1"),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed == {
        "current_dir": str(install_root / "current"),
        "current_exe": str(install_root / "current" / "apm.exe"),
        "junction_target": str(install_root / "releases" / "v99.0.0"),
        "executable_checked": True,
        "path_entries": [str(install_root / "current"), str(install_root / "bin")],
    }
    assert not install_root.exists()


@pytest.mark.parametrize("surface", SURFACES)
def test_metadata_redirects_explain_final_endpoint(surface: str, tmp_path: Path) -> None:
    """Never follow or echo an untrusted Location; name the operator's recovery action."""
    ok, calls, output = run_lookup(
        surface,
        tmp_path,
        [response(302, headers={"Location": f"https://user:{TOKEN}@redirect.example/latest.json"})],
        {"GITHUB_TOKEN": TOKEN, "APM_RELEASE_METADATA_URL": "https://mirror.example/latest.json"},
    )
    assert not ok
    assert "redirects are not followed" in output
    assert "APM_RELEASE_METADATA_URL" in output
    assert "final JSON endpoint" in output
    assert TOKEN not in output
    assert [call["authenticated"] for call in calls] == [False]


@pytest.mark.parametrize("channel", ["stable", "prerelease"])
def test_malformed_github_host_stays_quiet_or_structured(channel: str) -> None:
    """The owner translates URL parsing errors before transport or token resolution."""
    with (
        patch.dict(os.environ, {"GITHUB_URL": "https://[invalid"}, clear=True),
        patch("requests.get", side_effect=AssertionError("network forbidden")),
    ):
        assert get_latest_version_from_github(include_prerelease=channel == "prerelease") is None
        with pytest.raises(ReleaseMetadataError, match="Check GITHUB_URL"):
            get_latest_version_for_self_update(channel)


def _metadata_helper() -> str:
    """Load the production helper without running installation side effects."""
    source = (ROOT / "install.sh").read_text(encoding="utf-8")
    body = source.split("fetch_release_metadata() {", 1)[1].split(
        "metadata_is_rate_limited() {", 1
    )[0]
    return "fetch_release_metadata() {" + body


@pytest.mark.skipif(sys.platform == "win32", reason="Unix shell installer")
@pytest.mark.parametrize("mode", ["anonymous", "authenticated"])
def test_real_curl_metadata_errors_never_expose_url_credentials(mode: str, tmp_path: Path) -> None:
    """Real curl rejects a malformed URL with all network protocols disabled."""
    curl = shutil.which("curl")
    assert curl is not None
    script = (
        'curl() { "$REAL_CURL" -q --proto =file "$@"; }\n'
        + _metadata_helper()
        + '\nLATEST_RELEASE_URL="https://synthetic-user:$EXPECTED_TOKEN@mirror.example/["\n'
        + 'AUTH_HEADER_VALUE="$EXPECTED_TOKEN"\n'
        + f"fetch_release_metadata {mode}\n"
        + 'printf "curl_exit=%s\\n" "$CURL_EXIT_CODE"\n'
    )
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        env={"PATH": os.defpath, "HOME": str(tmp_path), "REAL_CURL": curl, "EXPECTED_TOKEN": TOKEN},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == "curl_exit=3\n"
    assert result.stderr == ""


@pytest.mark.skipif(sys.platform == "win32", reason="Unix shell installer")
def test_large_multiline_metadata_preserves_final_response(tmp_path: Path) -> None:
    """Streaming past interim headers must retain every line of a valid final body."""
    payload = response()
    payload.update({"interim": True, "indent": 2})
    payload["body"]["notes"] = [f"release note {index}" for index in range(1000)]
    ok, calls, output = run_lookup("unix", tmp_path, [payload], {"GITHUB_TOKEN": TOKEN})
    assert ok, output
    assert [call["authenticated"] for call in calls] == [True]


@pytest.mark.benchmark
@pytest.mark.skipif(sys.platform == "win32", reason="Unix shell installer")
def test_metadata_parser_scales_linearly(tmp_path: Path) -> None:
    """Median tenfold-input scaling includes the actual production parser and shell pipes."""
    script = (
        "curl() { cat; }\n"
        + _metadata_helper()
        + "\nfetch_release_metadata anonymous\n"
        + 'printf "%s\\n%s\\n%s\\n" "$METADATA_STATUS" "$METADATA_HEADERS" "$LATEST_RELEASE"\n'
    )
    timings = []
    for count in (2000, 20000):
        body = json.dumps({"tag_name": "v99.0.0", "notes": ["x" * 90] * count}, indent=2)
        payload = (
            "HTTP/1.1 103 Early Hints\r\nLink: </style.css>\r\n\r\nHTTP/1.1 200 OK\r\n\r\n"
            + body
            + "\n200\n"
        )
        samples = []
        for _ in range(5):
            started = time.perf_counter()
            result = subprocess.run(
                ["/bin/bash", "-c", script],
                input=payload,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
                env={"PATH": os.defpath, "HOME": str(tmp_path)},
            )
            samples.append(time.perf_counter() - started)
            assert result.returncode == 0
            assert result.stdout == "200\nHTTP/1.1 200 OK\n\n" + body + "\n"
        timings.append(statistics.median(samples))
    assert timings[1] / timings[0] < 15, timings
