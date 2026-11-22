"""The simulated ADO chain must not leak its bearer patch across threads (#4762)."""

import threading
from unittest.mock import MagicMock

import pytest

from apm_cli.core import azure_cli
from tests.utils.simulated_ado_chain import install_simulated_ado_chain

pytestmark = pytest.mark.unit


def test_overlapping_calls_restore_the_real_bearer_provider():
    """Two threads overlap inside the chain; the module global ends up real.

    Thread A waits inside its operation until B has entered, and B waits until
    A has returned, which is the enter-A, enter-B, exit-A, exit-B order that
    left ``get_bearer_provider`` bound to a MagicMock and reddened the ADO
    error-context tests later in the same xdist worker. The waits time out,
    so a chain that serializes the calls still finishes.
    """
    real = azure_cli.get_bearer_provider
    auth = MagicMock()
    auth.resolve.return_value = MagicMock(token="pat", auth_scheme="basic", source="ADO_APM_PAT")
    install_simulated_ado_chain(auth)
    a_inside, b_inside, a_done = threading.Event(), threading.Event(), threading.Event()

    def op_a(_token, _env):
        a_inside.set()
        b_inside.wait(timeout=1)
        return "a"

    def op_b(_token, _env):
        b_inside.set()
        a_done.wait(timeout=1)
        return "b"

    def run_a():
        assert auth.try_with_fallback("dev.azure.com", op_a, org="contoso") == "a"
        a_done.set()

    def run_b():
        a_inside.wait(timeout=1)
        assert auth.try_with_fallback("dev.azure.com", op_b, org="contoso") == "b"

    threads = [threading.Thread(target=run_a), threading.Thread(target=run_b)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert azure_cli.get_bearer_provider is real
