"""Tests for ``real_git_tls_config`` (issue #2545).

Reads the ambient (non-isolated) ``http.sslBackend``/``http.sslCAInfo``/
``http.sslCAPath`` git config that lets a plain ``git clone`` succeed behind a TLS-inspecting proxy
whose intercepting CA chain has no revocation info.

The owner runs for real down to the ``git config`` child process: the fake
below stands in for that process only, answering with the bytes
``git config --null --show-scope --list`` prints.
"""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable
from types import SimpleNamespace

import pytest

from apm_cli.utils.git_env import real_git_tls_config, reset_git_cache

pytestmark = pytest.mark.unit


class _GitConfigChild:
    """A fake ``git config`` child: fixed reply, every start recorded."""

    def __init__(
        self,
        *entries: tuple[str, str, str],
        returncode: int = 0,
        start_error: OSError | None = None,
        before_reply: Callable[[], None] | None = None,
    ) -> None:
        self.stdout = b"".join(
            f"{scope}\0{key}\n{value}\0".encode() for scope, key, value in entries
        )
        self.returncode = returncode
        self.start_error = start_error
        self.before_reply = before_reply
        self.starts: list[tuple[list[str], dict[str, str]]] = []
        self._lock = threading.Lock()

    def popen(self, command: list[str], **kwargs: object) -> SimpleNamespace:
        with self._lock:
            self.starts.append((list(command), dict(kwargs["env"])))  # type: ignore[arg-type]
        if self.start_error is not None:
            raise self.start_error

        def communicate(timeout: float | None = None) -> tuple[bytes, bytes]:
            if self.before_reply is not None:
                self.before_reply()
            return self.stdout, b""

        return SimpleNamespace(
            returncode=self.returncode, communicate=communicate, kill=lambda: None
        )


def _global(*pairs: tuple[str, str]) -> tuple[tuple[str, str, str], ...]:
    return tuple(("global", key, value) for key, value in pairs)


@pytest.fixture(autouse=True)
def _fresh_cache():
    # real_git_tls_config() caches its env=None result at module scope, so
    # every test starts and ends with a cold cache (issue #2545 follow-up).
    reset_git_cache()
    yield
    reset_git_cache()


@pytest.fixture
def git_child(monkeypatch: pytest.MonkeyPatch) -> Callable[..., _GitConfigChild]:
    def install(*entries: tuple[str, str, str], **behavior: object) -> _GitConfigChild:
        child = _GitConfigChild(*entries, **behavior)  # type: ignore[arg-type]
        monkeypatch.setattr(subprocess, "Popen", child.popen)
        return child

    return install


class TestRealGitTlsConfig:
    def test_returns_ssl_backend_and_ca_info(self, git_child) -> None:
        git_child(
            *_global(("http.sslbackend", "schannel"), ("http.sslcainfo", "/etc/ssl/corp-ca.pem"))
        )
        assert real_git_tls_config() == {
            "http.sslbackend": "schannel",
            "http.sslcainfo": "/etc/ssl/corp-ca.pem",
        }

    def test_returns_ssl_ca_path(self, git_child) -> None:
        git_child(*_global(("http.sslcapath", "/etc/ssl/corp-certs")))
        assert real_git_tls_config() == {"http.sslcapath": "/etc/ssl/corp-certs"}

    def test_repository_scoped_entries_are_never_trusted(self, git_child) -> None:
        """Local/worktree config from a repository is not a trust source."""
        git_child(
            ("global", "http.sslcainfo", "/user/ca.pem"),
            ("local", "http.sslcainfo", "/repo/evil-ca.pem"),
            ("worktree", "http.sslcapath", "/repo/evil-certs"),
        )
        assert real_git_tls_config() == {"http.sslcainfo": "/user/ca.pem"}

    def test_ignores_unrelated_config_keys(self, git_child) -> None:
        """credential.helper and insteadOf rewrites are never read here."""
        git_child(
            *_global(
                ("credential.helper", "store"),
                ("url.file:///fixture/.insteadof", "https://github.com/acme"),
                ("http.sslbackend", "schannel"),
            )
        )
        assert real_git_tls_config() == {"http.sslbackend": "schannel"}

    def test_empty_value_excluded(self, git_child) -> None:
        git_child(*_global(("http.sslbackend", "")))
        assert real_git_tls_config() == {}

    def test_no_ambient_tls_config_returns_empty_mapping(self, git_child) -> None:
        git_child(*_global(("core.autocrlf", "true")))
        assert real_git_tls_config() == {}

    def test_higher_precedence_empty_value_overrides_earlier_non_empty(self, git_child) -> None:
        """A later (higher-precedence) empty value must win over an earlier
        non-empty one -- entries arrive lowest-to-highest precedence,
        matching Git's own resolution (issue #2545 follow-up)."""
        git_child(
            ("global", "http.sslcainfo", "/global/corp-ca.pem"),
            ("command", "http.sslcainfo", ""),
        )
        assert real_git_tls_config() == {}

    def test_higher_precedence_non_empty_value_overrides_earlier_non_empty(self, git_child) -> None:
        git_child(
            ("system", "http.sslbackend", "openssl"),
            ("global", "http.sslbackend", "schannel"),
        )
        assert real_git_tls_config() == {"http.sslbackend": "schannel"}

    def test_probe_error_returns_empty_mapping(self, git_child) -> None:
        """A best-effort probe failure never blocks the anonymous attempt."""
        child = git_child(returncode=128)
        assert real_git_tls_config() == {}
        assert len(child.starts) == 1

    def test_malformed_probe_output_returns_empty_mapping(self, git_child) -> None:
        child = git_child()
        child.stdout = b"global\0no-newline-in-payload\0"
        assert real_git_tls_config() == {}

    def test_oserror_returns_empty_mapping(self, git_child) -> None:
        git_child(start_error=OSError("git not found"))
        assert real_git_tls_config() == {}

    def test_caller_env_reaches_git_sanitized(self, git_child) -> None:
        child = git_child()
        real_git_tls_config({"PATH": "/usr/bin", "GIT_DIR": "/invoking/.git"})

        ((command, env),) = child.starts
        assert command[-4:] == ["config", "--null", "--show-scope", "--list"]
        assert env["PATH"] == "/usr/bin"
        assert env["GIT_TRACE_REDACT"] == "1"
        assert "GIT_DIR" not in env


class TestRealGitTlsConfigCaching:
    """issue #2545 follow-up: avoid one ``git config`` child process per
    dependency in a large install -- cache the env=None probe once."""

    def test_second_call_does_not_reprobe(self, git_child) -> None:
        child = git_child(*_global(("http.sslbackend", "schannel")))
        first = real_git_tls_config()
        second = real_git_tls_config()

        assert first == {"http.sslbackend": "schannel"}
        assert second == {"http.sslbackend": "schannel"}
        assert len(child.starts) == 1

    def test_empty_result_is_cached_too(self, git_child) -> None:
        """An empty ambient config is a real, stable answer -- not a
        signal to keep retrying the probe on every subsequent call."""
        child = git_child()
        assert real_git_tls_config() == {}
        assert real_git_tls_config() == {}
        assert len(child.starts) == 1

    def test_probe_failure_is_cached_too(self, git_child) -> None:
        """A failed probe is also cached -- retrying the same slow/failing
        subprocess on every attempt would be worse than a stale {}."""
        child = git_child(start_error=OSError("git not found"))
        assert real_git_tls_config() == {}
        assert real_git_tls_config() == {}
        assert len(child.starts) == 1

    def test_explicit_env_always_reprobes(self, git_child) -> None:
        """Callers that pass an explicit env (tests, sandboxed probes)
        bypass the cache -- only the default env=None case is cached."""
        child = git_child(*_global(("http.sslbackend", "schannel")))
        real_git_tls_config({"PATH": "/usr/bin"})
        real_git_tls_config({"PATH": "/usr/bin"})
        assert len(child.starts) == 2

    def test_returned_mapping_is_a_copy(self, git_child) -> None:
        """Mutating a caller's returned dict must not corrupt the cache."""
        git_child(*_global(("http.sslbackend", "schannel")))
        first = real_git_tls_config()
        first["http.sslbackend"] = "corrupted"
        assert real_git_tls_config() == {"http.sslbackend": "schannel"}

    def test_reset_git_cache_forces_reprobe(self, git_child) -> None:
        child = git_child(*_global(("http.sslbackend", "schannel")))
        real_git_tls_config()
        reset_git_cache()
        real_git_tls_config()
        assert len(child.starts) == 2

    def test_concurrent_first_calls_share_one_probe(self, git_child) -> None:
        """Parallel installs call this from multiple worker threads; the
        very first (cold-cache) burst must still probe exactly once, not
        once per racing thread (issue #2545 follow-up)."""
        from concurrent.futures import ThreadPoolExecutor

        probe_started = threading.Event()
        release_probe = threading.Event()

        def hold_the_probe() -> None:
            probe_started.set()
            release_probe.wait(timeout=5)

        child = git_child(*_global(("http.sslbackend", "schannel")), before_reply=hold_the_probe)
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(real_git_tls_config) for _ in range(8)]
            assert probe_started.wait(timeout=5)
            # Give any racing (unguarded) second probe a chance to start
            # before releasing the first one -- this is what the lock
            # must prevent.
            threading.Event().wait(0.05)
            release_probe.set()
            results = [f.result(timeout=5) for f in futures]

        assert len(child.starts) == 1
        assert all(r == {"http.sslbackend": "schannel"} for r in results)
