"""Regression tests for inconclusive GitHub validation probes."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from apm_cli.install import validation

PROBE_PATHS = ("primary", "parse-fallback")


def _response(status_code: int, headers: dict[str, str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        ok=False,
        status_code=status_code,
        reason="test response",
        headers=headers or {},
    )


def _resolver() -> MagicMock:
    resolver = MagicMock()
    resolver.classify_host.return_value = SimpleNamespace(
        api_base="https://api.github.com",
        display_name="github.com",
        kind="github",
        has_public_repos=True,
    )
    resolver.resolve.return_value = SimpleNamespace(
        source="test",
        token_type="pat",
        token="test-token",
    )
    resolver.build_error_context.return_value = "auth diagnostics"

    def _retry_after_unauthenticated_failure(host, operation, **kwargs):
        try:
            return operation(None, {})
        except Exception:
            return operation("test-token", {})

    resolver.try_with_fallback.side_effect = _retry_after_unauthenticated_failure
    return resolver


def _run_probe(
    probe_path: str,
    request_result: object | list[object],
    *,
    verbose: bool = False,
) -> tuple[bool, MagicMock, MagicMock]:
    resolver = _resolver()
    logger = MagicMock()
    logger.verbose = verbose
    verbose_log = logger.verbose_detail if verbose else None
    request_results = (
        request_result if isinstance(request_result, list) else [request_result, request_result]
    )

    with (
        patch(
            "apm_cli.install.validation.requests.get",
            side_effect=request_results,
        ),
        patch("apm_cli.deps.registry_proxy.is_enforce_only", return_value=False),
    ):
        if probe_path == "primary":
            dep_ref = SimpleNamespace(host="github.com", port=None, repo_url="owner/repo")
            result = validation._validate_github_package(
                dep_ref,
                resolver,
                verbose,
                verbose_log,
                "owner/repo",
                logger,
            )
        else:
            result = validation._validate_parse_failure_fallback(
                "owner/repo",
                resolver,
                verbose_log,
                logger,
            )

    return result, resolver, logger


@pytest.mark.parametrize("probe_path", PROBE_PATHS)
@pytest.mark.parametrize(
    "transport_error",
    [
        requests.exceptions.ConnectionError("connection failed"),
        requests.exceptions.Timeout("request timed out"),
    ],
    ids=["connection-error", "timeout"],
)
def test_transport_failure_defers_to_authoritative_download(
    probe_path: str,
    transport_error: requests.exceptions.RequestException,
) -> None:
    result, _resolver_mock, logger = _run_probe(probe_path, transport_error, verbose=True)

    assert result is True
    assert any(
        "download" in call.args[0] and "inconclusive" in call.args[0]
        for call in logger.verbose_detail.call_args_list
    )
    assert all("test-token" not in call.args[0] for call in logger.verbose_detail.call_args_list)
    logger.warning.assert_not_called()
    logger.info.assert_not_called()


@pytest.mark.parametrize("probe_path", PROBE_PATHS)
@pytest.mark.parametrize("status_code", [408, 429, 500, 502, 503, 504])
def test_transient_http_status_defers_to_authoritative_download(
    probe_path: str,
    status_code: int,
) -> None:
    result, _resolver_mock, _logger = _run_probe(probe_path, _response(status_code))

    assert result is True


@pytest.mark.parametrize("probe_path", PROBE_PATHS)
@pytest.mark.parametrize("status_code", [401, 403, 404])
def test_non_transient_http_status_fails_closed(
    probe_path: str,
    status_code: int,
) -> None:
    result, resolver, _logger = _run_probe(
        probe_path,
        _response(status_code, {"X-RateLimit-Remaining": "10"}),
    )

    assert result is False
    resolver.build_error_context.assert_not_called()


@pytest.mark.parametrize("probe_path", PROBE_PATHS)
def test_final_status_after_auth_fallback_controls_classification(probe_path: str) -> None:
    transient_result, _resolver_mock, _logger = _run_probe(
        probe_path,
        [_response(404), _response(503)],
    )
    closed_result, _resolver_mock, _logger = _run_probe(
        probe_path,
        [_response(503), _response(404)],
    )

    assert transient_result is True
    assert closed_result is False


@pytest.mark.parametrize("probe_path", PROBE_PATHS)
def test_closed_status_keeps_verbose_auth_diagnostics(probe_path: str) -> None:
    result, resolver, logger = _run_probe(probe_path, _response(401), verbose=True)

    assert result is False
    resolver.build_error_context.assert_called_once()
    assert any(call.args[0] == "auth diagnostics" for call in logger.verbose_detail.call_args_list)


@pytest.mark.parametrize("probe_path", PROBE_PATHS)
def test_tls_failure_fails_closed(probe_path: str) -> None:
    result, resolver, logger = _run_probe(
        probe_path,
        requests.exceptions.SSLError("certificate validation failed"),
    )

    assert result is False
    resolver.build_error_context.assert_not_called()
    logger.warning.assert_called_once()


@pytest.mark.parametrize("probe_path", PROBE_PATHS)
def test_confirmed_throttle_keeps_existing_download_defer(probe_path: str) -> None:
    result, _resolver_mock, logger = _run_probe(
        probe_path,
        _response(403, {"X-RateLimit-Remaining": "0"}),
    )

    assert result is True
    logger.info.assert_called_once()


@pytest.mark.parametrize("probe_path", PROBE_PATHS)
def test_unknown_failure_fails_closed(probe_path: str) -> None:
    result, _resolver_mock, _logger = _run_probe(
        probe_path,
        ValueError("unexpected response handling failure"),
    )

    assert result is False


@pytest.mark.parametrize("probe_path", PROBE_PATHS)
def test_transient_detail_is_verbose_only(probe_path: str) -> None:
    result, _resolver_mock, logger = _run_probe(probe_path, _response(503))

    assert result is True
    logger.verbose_detail.assert_not_called()
    logger.warning.assert_not_called()
    logger.info.assert_not_called()
