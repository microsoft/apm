"""git's schannel trust, in process: the Windows store only, revocation checked (W3d SC1-SC8).

Default Git for Windows sets ``http.sslBackend=schannel`` system-wide. The
in-process fetch and ref listing then verify through ``truststore`` against
the same Windows store, with no extra anchors and revocation checked for
the whole chain; any doubt falls back to git.
"""

from __future__ import annotations

import ssl
from collections import namedtuple

import pytest

from apm_cli.deps import anonymous_ref_listing as arl

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-git-transport-selection")]

Entry = namedtuple("Entry", "key value scope", defaults=("global",))


@pytest.fixture
def on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.name", "nt")


def test_schannel_without_a_ca_source_trusts_the_windows_store(on_windows) -> None:
    assert arl._verify({}, {"http.sslbackend": "schannel"}) == arl.OS_TRUST
    assert arl.requests_verify(arl.OS_TRUST) is True


@pytest.mark.parametrize(
    ("env", "config"),
    [
        ({"GIT_SSL_CAINFO": "/ca.pem"}, {}),
        ({"SSL_CERT_FILE": "/ca.pem"}, {}),
        ({}, {"http.sslcainfo": "/ca.pem"}),
    ],
)
def test_schannel_ignores_ca_files_as_git_does(on_windows, env, config) -> None:
    """git clears curl's CA file under schannel unless schannelUseSSLCAInfo is set."""
    assert arl._verify(env, {"http.sslbackend": "schannel", **config}) == arl.OS_TRUST


def test_schannel_spelled_otherwise_falls_back(on_windows) -> None:
    """git's strcmp is case-sensitive: "Schannel" keeps the CA file, so git decides."""
    with pytest.raises(arl._Fallback, match="not spelled 'schannel'"):
        arl._verify({}, {"http.sslbackend": "Schannel", "http.sslcainfo": "/ca.pem"})


def test_schannel_off_windows_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.name", "posix")
    with pytest.raises(arl._Fallback, match="outside Windows"):
        arl._verify({}, {"http.sslbackend": "schannel"})


@pytest.mark.parametrize(
    "key", ["http.schannelCheckRevoke", "http.schannelUseSSLCAInfo", "http.sslCAPath"]
)
def test_any_other_schannel_or_tls_key_falls_back(key: str) -> None:
    entries = [Entry("http.sslBackend", "schannel"), Entry(key, "false")]
    assert arl._classify(entries) is None


def test_the_context_adds_no_anchor_and_checks_host_chain_and_revocation() -> None:
    import certifi

    context = arl.os_trust_context()
    context.load_verify_locations(cafile=certifi.where())  # urllib3 does this for verify=True
    context.load_default_certs()
    context.set_default_verify_paths()

    assert context._ctx.get_ca_certs() == []  # SC1: the Windows store is the only anchor
    assert context.check_hostname is True  # SC5
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.verify_flags & ssl.VERIFY_CRL_CHECK_CHAIN


def test_truststore_is_new_enough() -> None:
    import truststore

    version = tuple(int(part) for part in truststore.__version__.split(".")[:3])
    assert version >= (0, 10, 4)  # SC3


def test_one_os_trust_session_per_command_dropped_after_user_code() -> None:
    from apm_cli.core.invocation_memo import forget_invocation_memos, invocation_scope

    with invocation_scope():
        first = arl._session_for_refs(arl.OS_TRUST)
        assert arl._session_for_refs(arl.OS_TRUST) is first  # SC8: keep-alive in a command
        forget_invocation_memos()  # a lifecycle script ran
        assert arl._session_for_refs(arl.OS_TRUST) is not first
    assert arl._session_for_refs(arl.OS_TRUST) is not arl._session_for_refs(arl.OS_TRUST)
