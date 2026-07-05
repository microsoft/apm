"""Verify HTTPS against the OS trust store by default.

``requests`` verifies against the bundled ``certifi`` set, which lacks
internal/corporate root CAs and TLS-proxy certs. Because APM also shells out to
``git`` (which reads the OS trust store), ``git clone`` of an internal host
succeeds while APM's ``requests`` calls fail on the same chain. This routes
``requests`` through the OS store via ``truststore`` so the two agree, with no
per-shell config.

Best-effort -- ``configure_tls_trust`` never raises:

* An explicit ``REQUESTS_CA_BUNDLE`` / ``CURL_CA_BUNDLE`` wins (no injection).
* Missing ``truststore`` or a failed injection falls back to ``certifi``.
* ``APM_DISABLE_TRUSTSTORE`` forces the legacy ``certifi``-only behaviour.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

logger = logging.getLogger(__name__)

# The CA-bundle env vars ``requests`` honours (via merge_environment_settings);
# when one is set, respect that pinned bundle and skip injection. SSL_CERT_FILE
# and SSL_CERT_DIR are excluded on purpose: requests ignores those standalone
# variables, and the frozen binary's runtime hook sets SSL_CERT_FILE to bundled
# certifi -- treating either as an override would make injection a no-op in the
# shipped artifact.
_EXPLICIT_CA_ENV_VARS = ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")

# Escape hatch: set truthy to force the legacy certifi-only behaviour.
_DISABLE_ENV_VAR = "APM_DISABLE_TRUSTSTORE"

_TRUTHY = {"1", "true", "yes", "on"}


def _env_flag(name: str, env: Mapping[str, str] | None = None) -> bool:
    environ = os.environ if env is None else env
    return environ.get(name, "").strip().lower() in _TRUTHY


def has_explicit_ca_override(env: Mapping[str, str] | None = None) -> bool:
    """Return True when requests has an explicit CA bundle override."""
    environ = os.environ if env is None else env
    return any((environ.get(var) or "").strip() for var in _EXPLICIT_CA_ENV_VARS)


def configure_tls_trust(env: Mapping[str, str] | None = None) -> bool:
    """Route HTTPS verification through the OS trust store when possible.

    Call once at process startup, before the first HTTPS request. Returns
    ``True`` when ``truststore`` was injected, ``False`` when the default
    ``certifi`` behaviour was left in place (explicit override, opt-out,
    ``truststore`` missing, or injection failure). Never raises.
    """
    if _env_flag(_DISABLE_ENV_VAR, env):
        logger.debug("OS trust-store injection disabled via %s", _DISABLE_ENV_VAR)
        return False

    if has_explicit_ca_override(env):
        logger.debug("Explicit CA bundle set; leaving certifi/verify path intact")
        return False

    try:
        # Broad except: a broken/incompatible install can fail at import, not
        # only with ImportError -- degrade instead of crashing startup.
        import truststore
    except Exception as exc:
        logger.debug("truststore unavailable (%s); verifying TLS against bundled certifi", exc)
        return False

    try:
        truststore.inject_into_ssl()
    except Exception as exc:
        logger.debug("truststore.inject_into_ssl() failed (%s); falling back to certifi", exc)
        return False

    logger.debug("Verifying TLS against the OS trust store via truststore")
    return True
