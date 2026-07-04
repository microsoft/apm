# PyInstaller runtime hook -- configures TLS trust for the frozen binary so
# HTTPS connections work on every platform without requiring the user to
# install Python or set environment variables.
#
# Problem: requests defaults to certifi, while enterprise machines typically
# install corporate or internal PKI roots into the OS trust store used by git,
# curl, and browsers. Frozen binaries also have brittle OpenSSL default paths.
#
# Solution: Use truststore first so urllib3/requests/stdlib HTTPS share the OS
# trust store. If truststore is unavailable, fall back to the bundled certifi
# CA bundle so frozen binaries still have a working public-web baseline.
#
# This hook executes before any application code so the variables are
# visible to every subsequent import.

import os
import sys

_CA_OVERRIDE_ENV_VARS = (
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)


def _has_explicit_ca_override() -> bool:
    """Return True when the user explicitly selected a CA bundle/path."""
    return any((os.environ.get(name) or "").strip() for name in _CA_OVERRIDE_ENV_VARS)


def _inject_system_trust_store() -> bool:
    """Inject truststore into stdlib SSL if available."""
    try:
        import truststore

        truststore.inject_into_ssl()
        return True
    except Exception:
        return False


def _configure_ssl_certs() -> None:
    """Configure TLS trust for frozen binaries."""
    if not getattr(sys, "frozen", False):
        return

    # Honour explicit user overrides -- never clobber them.
    if _has_explicit_ca_override():
        return

    if _inject_system_trust_store():
        return

    try:
        import certifi
        ca_bundle = certifi.where()
        if os.path.isfile(ca_bundle):
            os.environ["SSL_CERT_FILE"] = ca_bundle
    except Exception:
        # certifi unavailable or broken -- fall through to system defaults.
        pass


_configure_ssl_certs()
