"""Default TLS trust configuration for APM's HTTP layer.

By default ``requests`` verifies HTTPS against the bundled ``certifi`` CA set,
which does **not** contain internal/corporate root CAs or the certificates
injected by a TLS-inspecting proxy (Zscaler, Netskope, Palo Alto, ...). APM
also shells out to ``git``, which reads the OS trust store, so ``git clone``
of an internal host succeeds while APM's ``requests``-based Contents API calls
fail against the *same* certificate chain -- a confusing, inconsistent
failure for enterprise users.

This module opts APM into the OS trust store via `truststore
<https://pypi.org/project/truststore/>`_ so both paths verify against the
same source with zero per-shell configuration. It is deliberately
best-effort:

* If the user has pinned an explicit CA bundle (``REQUESTS_CA_BUNDLE`` /
  ``CURL_CA_BUNDLE`` / ``SSL_CERT_FILE``), that choice wins and we do not
  override it.
* If ``truststore`` is unavailable or injection raises for any reason, APM
  silently falls back to the previous ``certifi`` behaviour.
* ``APM_DISABLE_TRUSTSTORE`` forces the old behaviour as an escape hatch.

``configure_tls_trust`` never raises: TLS setup must not be able to crash CLI
startup.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Env vars through which a user pins an explicit CA bundle. When any is set we
# respect that choice rather than silently redirecting verification to the OS
# trust store (which could ignore a deliberately narrow, air-gapped bundle).
_EXPLICIT_CA_ENV_VARS = ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE")

# Escape hatch: set truthy to force the legacy certifi-only behaviour.
_DISABLE_ENV_VAR = "APM_DISABLE_TRUSTSTORE"

_TRUTHY = {"1", "true", "yes", "on"}


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def configure_tls_trust() -> bool:
    """Route HTTPS verification through the OS trust store when possible.

    Call once at process startup, before the first HTTPS request. Returns
    ``True`` when ``truststore`` was injected, ``False`` when the default
    ``certifi`` behaviour was left in place (explicit override, opt-out,
    ``truststore`` missing, or injection failure). Never raises.
    """
    if _env_flag(_DISABLE_ENV_VAR):
        logger.debug("OS trust-store injection disabled via %s", _DISABLE_ENV_VAR)
        return False

    explicit = next((var for var in _EXPLICIT_CA_ENV_VARS if os.environ.get(var)), None)
    if explicit:
        # The user asked for a specific bundle; honour it verbatim.
        logger.debug("Explicit CA bundle set via %s; leaving certifi/verify path intact", explicit)
        return False

    try:
        import truststore
    except ImportError:
        logger.debug("truststore not installed; verifying TLS against bundled certifi")
        return False

    try:
        # Broad by design: trust setup must never crash CLI startup, so any
        # failure degrades to the certifi default rather than propagating.
        truststore.inject_into_ssl()
    except Exception as exc:
        logger.debug("truststore.inject_into_ssl() failed (%s); falling back to certifi", exc)
        return False

    logger.debug("Verifying TLS against the OS trust store via truststore")
    return True
