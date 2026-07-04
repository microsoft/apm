"""TLS trust-store bootstrap helpers for the APM CLI."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

_CA_OVERRIDE_ENV_VARS = (
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)


def has_explicit_ca_override(env: Mapping[str, str] | None = None) -> bool:
    """Return True when the user explicitly selected a CA bundle/path."""
    environ = os.environ if env is None else env
    return any((environ.get(name) or "").strip() for name in _CA_OVERRIDE_ENV_VARS)


def configure_system_trust_store(env: Mapping[str, str] | None = None) -> bool:
    """Make Python HTTPS clients use the OS trust store by default.

    APM is an application, so using truststore's stdlib injection is appropriate
    as long as it happens before network libraries construct SSL contexts.
    Explicit CA environment variables continue to win for edge cases.
    """
    if has_explicit_ca_override(env):
        return False

    try:
        import truststore
    except ImportError as exc:
        logging.getLogger(__name__).debug("truststore unavailable: %s", exc)
        return False

    try:
        truststore.inject_into_ssl()
    except Exception as exc:  # pragma: no cover - defensive against platform APIs
        logging.getLogger(__name__).debug("truststore injection failed: %s", exc)
        return False

    return True
