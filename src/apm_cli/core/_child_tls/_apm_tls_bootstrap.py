"""Self-contained OS-trust bootstrap for child runtimes. NO apm_cli dependency.

Depends only on ``truststore`` (installed into the child venv). Executed at
interpreter startup via a ``.pth`` file dropped into the child venv's
site-packages, so trust is delivered at venv-setup time rather than by
mutating the child's ``PYTHONPATH`` at spawn time (which would shadow a
user/corporate ``sitecustomize.py``).

Must stay SILENT (write nothing to stdout/stderr) and never raise -- a broken
bootstrap must not disturb the child runtime's own output or startup.
"""

import contextlib as _contextlib
import os as _os
import ssl as _ssl
import stat as _stat
import sys as _sys

_MAX_EXTRA_CA_BUNDLE_BYTES = 8 * 1024 * 1024
_DERIVED_REQUESTS_CA_MARKER = "APM_REQUESTS_CA_BUNDLE_IS_DERIVED_ADDITIVE"
_MISSING_TLS_REFERENCE = object()


def _truthy(val):
    return (val or "").strip().lower() in ("1", "true", "yes", "on")


def _same_path(left, right):
    try:
        return _os.path.normcase(_os.path.abspath(_os.path.expanduser(left))) == _os.path.normcase(
            _os.path.abspath(_os.path.expanduser(right))
        )
    except Exception:
        return False


def _is_pip_process():
    """Let pip use its vendored truststore without a second SSL injection."""
    if _os.path.basename(_sys.argv[0]).lower().startswith("pip"):
        return True
    original_args = getattr(_sys, "orig_argv", ())
    return any(
        original_args[index : index + 2] == ["-m", "pip"] for index in range(len(original_args) - 1)
    )


def _read_extra_ca_bundle():
    """Return one validated PEM snapshot, or None when unset/unusable."""
    path = (_os.environ.get("APM_EXTRA_CA_BUNDLE") or "").strip()
    if not path:
        return None
    try:
        with open(path, "rb") as handle:
            metadata = _os.fstat(handle.fileno())
            if not _stat.S_ISREG(metadata.st_mode):
                return None
            if metadata.st_size <= 0 or metadata.st_size > _MAX_EXTRA_CA_BUNDLE_BYTES:
                return None
            bundle_bytes = handle.read(_MAX_EXTRA_CA_BUNDLE_BYTES + 1)
        if len(bundle_bytes) > _MAX_EXTRA_CA_BUNDLE_BYTES:
            return None
        return bundle_bytes.decode("ascii")
    except Exception:
        return None


def _capture_tls_publication_state():
    """Capture loaded globals without importing the HTTP stack early."""
    urllib3_ssl = _sys.modules.get("urllib3.util.ssl_")
    requests_adapters = _sys.modules.get("requests.adapters")
    urllib3_context = (
        getattr(urllib3_ssl, "SSLContext", _MISSING_TLS_REFERENCE)
        if urllib3_ssl is not None
        else _MISSING_TLS_REFERENCE
    )
    preloaded = (
        getattr(requests_adapters, "_preloaded_ssl_context", _MISSING_TLS_REFERENCE)
        if requests_adapters is not None
        else _MISSING_TLS_REFERENCE
    )
    return _ssl.SSLContext, urllib3_ssl, urllib3_context, requests_adapters, preloaded


def _restore_tls_publication_state(state):
    """Best-effort exact rollback after a partial truststore publication."""
    original_ssl, original_urllib3, original_urllib3_context, original_requests, preloaded = state
    with _contextlib.suppress(Exception):
        _ssl.SSLContext = original_ssl
    urllib3_ssl = original_urllib3 or _sys.modules.get("urllib3.util.ssl_")
    if urllib3_ssl is not None:
        with _contextlib.suppress(Exception):
            urllib3_ssl.SSLContext = (
                original_ssl
                if original_urllib3_context is _MISSING_TLS_REFERENCE
                else original_urllib3_context
            )
    requests_adapters = original_requests or _sys.modules.get("requests.adapters")
    if requests_adapters is not None:
        try:
            if original_requests is None:
                if hasattr(requests_adapters, "_preloaded_ssl_context"):
                    try:
                        restored_preloaded = requests_adapters.create_urllib3_context()
                        restored_preloaded.load_verify_locations(
                            requests_adapters.extract_zipped_paths(
                                requests_adapters.DEFAULT_CA_BUNDLE_PATH
                            )
                        )
                    except Exception:
                        restored_preloaded = None
                    requests_adapters._preloaded_ssl_context = restored_preloaded
            elif preloaded is _MISSING_TLS_REFERENCE:
                with _contextlib.suppress(AttributeError):
                    del requests_adapters._preloaded_ssl_context
            else:
                requests_adapters._preloaded_ssl_context = preloaded
        except Exception:
            pass


def _install_additive_context(truststore, bundle_pem):
    """Transactionally install an OS context class plus *bundle_pem*."""

    class _APMExtraCAContext(truststore.SSLContext):
        def __init__(self, protocol=None):
            super().__init__(protocol)
            self.load_verify_locations(cadata=bundle_pem)

    _APMExtraCAContext.__module__ = truststore.SSLContext.__module__
    candidate = _APMExtraCAContext(_ssl.PROTOCOL_TLS_CLIENT)
    if not candidate.check_hostname or candidate.verify_mode != _ssl.CERT_REQUIRED:
        raise RuntimeError("additive TLS context weakened peer verification")

    state = _capture_tls_publication_state()
    _original_ssl, urllib3_ssl, _urllib3_context, requests_adapters, preloaded = state
    try:
        _ssl.SSLContext = _APMExtraCAContext
        if urllib3_ssl is not None:
            urllib3_ssl.SSLContext = _APMExtraCAContext
        if requests_adapters is not None and preloaded is not _MISSING_TLS_REFERENCE:
            requests_adapters._preloaded_ssl_context = candidate
    except Exception:
        _restore_tls_publication_state(state)
        raise
    return True


def _bootstrap():
    if _is_pip_process() or _truthy(_os.environ.get("APM_DISABLE_TRUSTSTORE")):
        return
    derived_path = (_os.environ.get(_DERIVED_REQUESTS_CA_MARKER, "") or "").strip()
    requests_path = (_os.environ.get("REQUESTS_CA_BUNDLE") or "").strip()
    requests_is_derived = bool(
        derived_path and requests_path and _same_path(derived_path, requests_path)
    )
    if not requests_is_derived:
        _os.environ.pop(_DERIVED_REQUESTS_CA_MARKER, None)
    for var in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        if var == "REQUESTS_CA_BUNDLE" and requests_is_derived:
            continue
        if (_os.environ.get(var) or "").strip():
            return
    try:
        import truststore
    except Exception:
        return
    extra_ca_pem = _read_extra_ca_bundle()
    if (_os.environ.get("APM_EXTRA_CA_BUNDLE") or "").strip() and extra_ca_pem is None:
        return
    marker = "APM_SSL_CERT_FILE_IS_BUNDLED_DEFAULT"
    bundled = None
    if _os.environ.get("SSL_CERT_FILE") and _truthy(_os.environ.get(marker)):
        bundled = _os.environ.pop("SSL_CERT_FILE", None)
    _os.environ.pop(marker, None)
    publication_state = _capture_tls_publication_state()
    try:
        truststore.inject_into_ssl()
        if extra_ca_pem is not None:
            _install_additive_context(truststore, extra_ca_pem)
    except Exception:
        _restore_tls_publication_state(publication_state)
        if bundled is not None:
            _os.environ["SSL_CERT_FILE"] = bundled


_bootstrap()
del _bootstrap
