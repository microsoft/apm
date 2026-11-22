"""The simulated ADO chain must not overlap its bearer patch across threads (#4762)."""

import contextlib
import threading
from unittest.mock import MagicMock

import pytest

import tests.utils.simulated_ado_chain as chain
from apm_cli.core import azure_cli

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-host-credential-resolution")]


def test_concurrent_calls_never_overlap_the_bearer_patch(monkeypatch):
    """B calls in while A is inside the chain; B's patch must wait for A's.

    Two overlapping patches of ``azure_cli.get_bearer_provider`` restore each
    other's MagicMock, leaving the module global mocked for the rest of the
    xdist worker (the ADO error-context flake). A holds its call open until B
    has started its own and then for a grace period, so an unserialized chain
    enters B's patch while A's is still active.
    """
    real_patch = chain.patch
    active, peak = [0], [0]
    counter_lock = threading.Lock()

    @contextlib.contextmanager
    def counting_patch(*args, **kwargs):
        with real_patch(*args, **kwargs) as patched:
            with counter_lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            try:
                yield patched
            finally:
                with counter_lock:
                    active[0] -= 1

    monkeypatch.setattr(chain, "patch", counting_patch)
    real = azure_cli.get_bearer_provider
    auth = MagicMock()
    auth.resolve.return_value = MagicMock(token="pat", auth_scheme="basic", source="ADO_APM_PAT")
    chain.install_simulated_ado_chain(auth)
    a_inside, b_calling = threading.Event(), threading.Event()
    results: dict[str, object] = {}

    def op_a(_token, _env):
        a_inside.set()
        assert b_calling.wait(timeout=10)
        threading.Event().wait(0.3)
        return "a"

    def call(name, op, before=None):
        try:
            if before is not None:
                before()
            results[name] = auth.try_with_fallback("dev.azure.com", op, org="contoso")
        except BaseException as exc:  # surfaced in the main thread below
            results[name] = exc

    def b_start():
        assert a_inside.wait(timeout=10)
        b_calling.set()

    threads = [
        threading.Thread(target=call, args=("a", op_a)),
        threading.Thread(target=call, args=("b", lambda _t, _e: "b", b_start)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not any(thread.is_alive() for thread in threads)
    assert results == {"a": "a", "b": "b"}
    assert peak[0] == 1
    assert azure_cli.get_bearer_provider is real
