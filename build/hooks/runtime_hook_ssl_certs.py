# PyInstaller runtime hook -- configures SSL certificate paths for the
# frozen binary so that HTTPS connections work on every platform without
# requiring the user to install Python or set environment variables.
#
# Problem: PyInstaller bundles OpenSSL, but the compiled-in certificate
# search path points at the *build machine's* Python framework directory
# (e.g. /Library/Frameworks/Python.framework/...).  On end-user machines
# that path rarely exists, causing SSL verification failures.
#
# Solution: Point ``SSL_CERT_FILE`` at the certifi CA bundle shipped
# inside the frozen binary.  ``requests``, ``urllib3``, and the stdlib
# ``ssl`` module all honour this variable.
#
# This hook executes before any application code so the variables are
# visible to every subsequent import.

import os
import sys


def _configure_ssl_certs() -> None:
    """Set SSL_CERT_FILE to the bundled certifi CA bundle when frozen."""
    if not getattr(sys, "frozen", False):
        return

    # Honour explicit user overrides -- never clobber them.
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE"):
        return

    try:
        import certifi
        ca_bundle = certifi.where()
        if os.path.isfile(ca_bundle):
            os.environ["SSL_CERT_FILE"] = ca_bundle
            # Marker: record that WE set this default (vs a genuine user
            # SSL_CERT_FILE). apm_cli.core.tls_trust reads this to know it may
            # pop SSL_CERT_FILE before truststore injection so the OS store is
            # used, restoring it only if injection fails (certifi fallback).
            os.environ["APM_SSL_CERT_FILE_IS_BUNDLED_DEFAULT"] = "1"
    except Exception:
        # certifi unavailable or broken -- fall through to system defaults.
        pass


_configure_ssl_certs()
