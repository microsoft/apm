"""Tests for ``real_git_tls_config`` (issue #2545).

Reads the ambient (non-isolated) ``http.sslBackend``/``http.sslCAInfo`` git
config that lets a plain ``git clone`` succeed behind a TLS-inspecting proxy
whose intercepting CA chain has no revocation info.
"""

from __future__ import annotations

import threading
from unittest.mock import patch

from apm_cli.utils.git_env import (
    GitConfigEntry,
    GitUrlRewriteProbeError,
    _GitConfigSnapshot,
    real_git_tls_config,
    reset_git_cache,
)


def _snapshot(*entries: tuple[str, str]) -> _GitConfigSnapshot:
    return _GitConfigSnapshot(
        tuple(GitConfigEntry("global", key, value) for key, value in entries),
        (),
        (),
    )


class TestRealGitTlsConfig:
    # issue #2545 follow-up: real_git_tls_config() caches its env=None
    # result at module scope (perf fix), so every test below that calls it
    # with the default env=None must start from a clean cache -- otherwise
    # whichever test runs first "wins" for the rest of the session.
    def setup_method(self) -> None:
        reset_git_cache()

    def teardown_method(self) -> None:
        reset_git_cache()

    def test_returns_ssl_backend_and_ca_info(self) -> None:
        snapshot = _snapshot(
            ("http.sslbackend", "schannel"),
            ("http.sslcainfo", "/etc/ssl/corp-ca.pem"),
        )
        with patch("apm_cli.utils.git_env._read_effective_git_config", return_value=snapshot):
            assert real_git_tls_config() == {
                "http.sslbackend": "schannel",
                "http.sslcainfo": "/etc/ssl/corp-ca.pem",
            }

    def test_ignores_unrelated_config_keys(self) -> None:
        """credential.helper and insteadOf rewrites are never read here."""
        snapshot = _snapshot(
            ("credential.helper", "store"),
            ("url.file:///fixture/.insteadof", "https://github.com/acme"),
            ("http.sslbackend", "schannel"),
        )
        with patch("apm_cli.utils.git_env._read_effective_git_config", return_value=snapshot):
            assert real_git_tls_config() == {"http.sslbackend": "schannel"}

    def test_empty_value_excluded(self) -> None:
        snapshot = _snapshot(("http.sslbackend", ""))
        with patch("apm_cli.utils.git_env._read_effective_git_config", return_value=snapshot):
            assert real_git_tls_config() == {}

    def test_no_ambient_tls_config_returns_empty_mapping(self) -> None:
        snapshot = _snapshot(("core.autocrlf", "true"))
        with patch("apm_cli.utils.git_env._read_effective_git_config", return_value=snapshot):
            assert real_git_tls_config() == {}

    def test_higher_precedence_empty_value_overrides_earlier_non_empty(self) -> None:
        """A later (higher-precedence) empty value must win over an earlier
        non-empty one -- ``entries`` is ordered lowest-to-highest precedence,
        matching Git's own resolution (issue #2545 follow-up)."""
        snapshot = _snapshot(
            ("http.sslcainfo", "/global/corp-ca.pem"),  # e.g. global scope
            ("http.sslcainfo", ""),  # e.g. local scope explicitly unsets it
        )
        with patch("apm_cli.utils.git_env._read_effective_git_config", return_value=snapshot):
            assert real_git_tls_config() == {}

    def test_higher_precedence_non_empty_value_overrides_earlier_non_empty(self) -> None:
        snapshot = _snapshot(
            ("http.sslbackend", "openssl"),
            ("http.sslbackend", "schannel"),
        )
        with patch("apm_cli.utils.git_env._read_effective_git_config", return_value=snapshot):
            assert real_git_tls_config() == {"http.sslbackend": "schannel"}

    def test_probe_error_returns_empty_mapping(self) -> None:
        """A best-effort probe failure never blocks the anonymous attempt."""
        with patch(
            "apm_cli.utils.git_env._read_effective_git_config",
            side_effect=GitUrlRewriteProbeError("probe timed out"),
        ):
            assert real_git_tls_config() == {}

    def test_oserror_returns_empty_mapping(self) -> None:
        with patch(
            "apm_cli.utils.git_env._read_effective_git_config",
            side_effect=OSError("git not found"),
        ):
            assert real_git_tls_config() == {}

    def test_passes_caller_env_through_to_git_subprocess_env(self) -> None:
        snapshot = _snapshot()
        with (
            patch(
                "apm_cli.utils.git_env.git_subprocess_env",
                return_value={"PATH": "/usr/bin"},
            ) as mock_sanitize,
            patch(
                "apm_cli.utils.git_env._read_effective_git_config",
                return_value=snapshot,
            ) as mock_read,
        ):
            real_git_tls_config({"PATH": "/usr/bin"})

        mock_sanitize.assert_called_once_with({"PATH": "/usr/bin"})
        mock_read.assert_called_once_with({"PATH": "/usr/bin"})


class TestRealGitTlsConfigCaching:
    """issue #2545 follow-up: avoid one ``git config`` child process per
    dependency in a large install -- cache the env=None probe once."""

    def setup_method(self) -> None:
        reset_git_cache()

    def teardown_method(self) -> None:
        reset_git_cache()

    def test_second_call_does_not_reprobe(self) -> None:
        snapshot = _snapshot(("http.sslbackend", "schannel"))
        with patch(
            "apm_cli.utils.git_env._read_effective_git_config",
            return_value=snapshot,
        ) as mock_read:
            first = real_git_tls_config()
            second = real_git_tls_config()

        assert first == {"http.sslbackend": "schannel"}
        assert second == {"http.sslbackend": "schannel"}
        mock_read.assert_called_once()

    def test_empty_result_is_cached_too(self) -> None:
        """An empty ambient config is a real, stable answer -- not a
        signal to keep retrying the probe on every subsequent call."""
        with patch(
            "apm_cli.utils.git_env._read_effective_git_config",
            return_value=_snapshot(),
        ) as mock_read:
            assert real_git_tls_config() == {}
            assert real_git_tls_config() == {}

        mock_read.assert_called_once()

    def test_probe_failure_is_cached_too(self) -> None:
        """A failed probe is also cached -- retrying the same slow/failing
        subprocess on every attempt would be worse than a stale {}."""
        with patch(
            "apm_cli.utils.git_env._read_effective_git_config",
            side_effect=OSError("git not found"),
        ) as mock_read:
            assert real_git_tls_config() == {}
            assert real_git_tls_config() == {}

        mock_read.assert_called_once()

    def test_explicit_env_always_reprobes(self) -> None:
        """Callers that pass an explicit env (tests, sandboxed probes)
        bypass the cache -- only the default env=None case is cached."""
        with patch(
            "apm_cli.utils.git_env._read_effective_git_config",
            return_value=_snapshot(("http.sslbackend", "schannel")),
        ) as mock_read:
            real_git_tls_config({"PATH": "/usr/bin"})
            real_git_tls_config({"PATH": "/usr/bin"})

        assert mock_read.call_count == 2

    def test_returned_mapping_is_a_copy(self) -> None:
        """Mutating a caller's returned dict must not corrupt the cache."""
        with patch(
            "apm_cli.utils.git_env._read_effective_git_config",
            return_value=_snapshot(("http.sslbackend", "schannel")),
        ):
            first = real_git_tls_config()
            first["http.sslbackend"] = "corrupted"
            second = real_git_tls_config()

        assert second == {"http.sslbackend": "schannel"}

    def test_reset_git_cache_forces_reprobe(self) -> None:
        with patch(
            "apm_cli.utils.git_env._read_effective_git_config",
            return_value=_snapshot(("http.sslbackend", "schannel")),
        ) as mock_read:
            real_git_tls_config()
            reset_git_cache()
            real_git_tls_config()

        assert mock_read.call_count == 2

    def test_concurrent_first_calls_share_one_probe(self) -> None:
        """Parallel installs call this from multiple worker threads; the
        very first (cold-cache) burst must still probe exactly once, not
        once per racing thread (issue #2545 follow-up)."""
        from concurrent.futures import ThreadPoolExecutor

        probe_started = threading.Event()
        release_probe = threading.Event()
        call_count = 0
        call_count_lock = threading.Lock()

        def _slow_read(_env: dict[str, str]) -> _GitConfigSnapshot:
            nonlocal call_count
            with call_count_lock:
                call_count += 1
            probe_started.set()
            release_probe.wait(timeout=5)
            return _snapshot(("http.sslbackend", "schannel"))

        with patch(
            "apm_cli.utils.git_env._read_effective_git_config",
            side_effect=_slow_read,
        ):
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(real_git_tls_config) for _ in range(8)]
                assert probe_started.wait(timeout=5)
                # Give any racing (unguarded) second probe a chance to start
                # before releasing the first one -- this is what the lock
                # must prevent.
                threading.Event().wait(0.05)
                release_probe.set()
                results = [f.result(timeout=5) for f in futures]

        assert call_count == 1
        assert all(r == {"http.sslbackend": "schannel"} for r in results)
