"""The scripted HTTPS proxy's RTT injection and traffic counters (perf fixtures).

``rtt_ms=0`` (the default) must leave the proxy's observable behaviour
unchanged; a positive RTT delays every tunnel and request; ``traffic()``
counts tunnels, requests and round trips either way.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import requests

from tests.utils.local_github_https_fixture import openssl_executable
from tests.utils.local_scripted_https_proxy import (
    HANDSHAKE_ROUND_TRIPS,
    ScriptedRequest,
    ScriptedResponse,
    local_scripted_https_proxy,
)

pytestmark = [
    pytest.mark.component,
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI required"),
]

_HOST = "origin.perf.test"


def _echo(request: ScriptedRequest) -> ScriptedResponse:
    status = 404 if request.path == "/missing" else 200
    return ScriptedResponse(status, {"Content-Type": "text/plain"}, request.path.encode())


def _exercise(directory: Path, **kwargs: float) -> tuple[list[tuple], dict[str, int], float]:
    with local_scripted_https_proxy(directory, {_HOST: _echo}, **kwargs) as proxy:
        proxies = {"https": proxy.proxy_url}
        start = time.perf_counter()
        with requests.Session() as session:
            for path in ("/a", "/b", "/missing"):
                session.get(
                    f"https://{_HOST}{path}",
                    proxies=proxies,
                    verify=str(proxy.ca_path),
                    timeout=30,
                )
        elapsed = time.perf_counter() - start
        observed = [(o.host, o.method, o.target, o.status, o.body) for o in proxy.observations]
        return observed, proxy.traffic().as_dict(), elapsed


def test_zero_rtt_keeps_observations_identical(tmp_path: Path) -> None:
    default, default_traffic, _ = _exercise(tmp_path / "default")
    explicit, explicit_traffic, _ = _exercise(tmp_path / "explicit", rtt_ms=0.0)

    assert explicit == default
    assert [(o[2], o[3]) for o in default] == [("/a", 200), ("/b", 200), ("/missing", 404)]
    assert (
        default_traffic
        == explicit_traffic
        == {
            "connects": 1,
            "requests": 3,
            "round_trips": HANDSHAKE_ROUND_TRIPS + 3,
        }
    )


def test_rtt_delays_each_round_trip(tmp_path: Path) -> None:
    rtt_ms = 40.0
    observed, traffic, elapsed = _exercise(tmp_path, rtt_ms=rtt_ms)

    assert len(observed) == 3
    assert elapsed >= traffic["round_trips"] * rtt_ms / 1000.0


def test_reset_traffic_and_negative_rtt(tmp_path: Path) -> None:
    with local_scripted_https_proxy(tmp_path, {_HOST: _echo}) as proxy:
        requests.get(
            f"https://{_HOST}/a",
            proxies={"https": proxy.proxy_url},
            verify=str(proxy.ca_path),
            timeout=30,
        )
        proxy.reset_traffic()
        assert proxy.traffic().as_dict() == {"connects": 0, "requests": 0, "round_trips": 0}
        assert len(proxy.observations) == 1
        with pytest.raises(ValueError, match="rtt_ms"):
            proxy.rtt_ms = -1
