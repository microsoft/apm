"""Self-contained OS-trust bootstrap for child runtimes. NO apm_cli dependency.

Depends only on ``truststore`` (installed into the child venv). Executed at
interpreter startup via a ``.pth`` file dropped into the child venv's
site-packages, so trust is delivered at venv-setup time rather than by
mutating the child's ``PYTHONPATH`` at spawn time (which would shadow a
user/corporate ``sitecustomize.py``).

Must stay SILENT (write nothing to stdout/stderr) and never raise -- a broken
bootstrap must not disturb the child runtime's own output or startup.
"""

import logging as _logging
import os as _os

_logger = _logging.getLogger("apm.tls")


def _truthy(val):
    return (val or "").strip().lower() in ("1", "true", "yes", "on")


def _bootstrap():
    if _truthy(_os.environ.get("APM_DISABLE_TRUSTSTORE")):
        return
    for var in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        if (_os.environ.get(var) or "").strip():
            return
    try:
        import truststore
    except Exception:
        return
    marker = "APM_SSL_CERT_FILE_IS_BUNDLED_DEFAULT"
    bundled = None
    if _os.environ.get("SSL_CERT_FILE") and _truthy(_os.environ.get(marker)):
        bundled = _os.environ.pop("SSL_CERT_FILE", None)
    _os.environ.pop(marker, None)
    try:
        truststore.inject_into_ssl()
        _logger.debug("TLS: child verifying against OS trust store")
    except Exception:
        if bundled is not None:
            _os.environ["SSL_CERT_FILE"] = bundled
        _logger.debug("TLS: child falling back to certifi")


_bootstrap()
del _bootstrap
