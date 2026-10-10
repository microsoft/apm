"""Verify HTTPS against the OS trust store by default.

``requests`` verifies against the bundled ``certifi`` set, which lacks
internal/corporate root CAs and TLS-proxy certs. Because APM also shells out to
``git`` (which reads the OS trust store), ``git clone`` of an internal host
succeeds while APM's ``requests`` calls fail on the same chain. This routes
``requests`` through the OS store via ``truststore`` so the two agree, with no
per-shell config.

Best-effort unless the operator selects an invalid additive bundle:

* An explicit ``REQUESTS_CA_BUNDLE`` / ``CURL_CA_BUNDLE`` wins (no injection).
* ``APM_EXTRA_CA_BUNDLE`` adds a validated PEM bundle to the selected defaults.
* Missing ``truststore`` or a failed injection falls back to ``certifi``.
* ``APM_DISABLE_TRUSTSTORE`` disables OS/additive trust without unsetting
  an independently configured Requests/curl replacement bundle.

The additive bundle applies to APM package-management HTTPS only.
Execution runtimes retain their existing trust configuration: the Python
``llm`` bootstrap still injects OS trust at venv setup, and Node/Rust use
their native settings. No additive settings are derived for children.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import os
import ssl
import stat
import sys
import tempfile
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Any, cast

from ..utils.path_security import PathTraversalError, ensure_path_within

logger = logging.getLogger(__name__)

# The CA-bundle env vars ``requests`` honours (via merge_environment_settings);
# when one is set, respect that pinned bundle and skip injection. SSL_CERT_FILE
# and SSL_CERT_DIR are excluded on purpose: requests ignores those standalone
# variables, and the frozen binary's runtime hook sets SSL_CERT_FILE to bundled
# certifi -- treating either as an override would make injection a no-op in the
# shipped artifact.
_EXPLICIT_CA_ENV_VARS = ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")

# Escape hatch: set truthy to disable APM's OS/additive trust.
# Explicit Requests/curl replacement variables remain authoritative.
_DISABLE_ENV_VAR = "APM_DISABLE_TRUSTSTORE"

# Additive CA bundle layered on top of the selected OS/certifi defaults.
_EXTRA_CA_ENV_VAR = "APM_EXTRA_CA_BUNDLE"

# Bound operator-provided CA input before reading it into memory. Enterprise
# bundles are normally well below 1 MiB; 8 MiB leaves ample room without
# allowing a device or unexpectedly huge file to consume unbounded memory.
_MAX_EXTRA_CA_BUNDLE_BYTES = 8 * 1024 * 1024

# stdlib ``ssl`` CA-file variable. truststore's Linux backend calls
# ``ctx.set_default_verify_paths()`` which honours SSL_CERT_FILE, so a bundled
# certifi value would shadow the OS store -- we pop it before injecting.
_SSL_CERT_FILE_VAR = "SSL_CERT_FILE"

# Marker set by build/hooks/runtime_hook_ssl_certs.py: records that the frozen
# binary set SSL_CERT_FILE to bundled certifi (vs a genuine user value).
_BUNDLED_CERT_MARKER = "APM_SSL_CERT_FILE_IS_BUNDLED_DEFAULT"

# Directory (relative to the installed package) that holds the child bootstrap.
_CHILD_SHIM_DIRNAME = "_child_tls"

# The two artifacts delivered into a child venv's site-packages to deliver
# OS-trust at interpreter startup (see ensure_child_tls_bootstrap). The module
# ships as a data file; the .pth is generated inline (its content is trivial and
# setuptools' packages.find omits stray .pth files from the wheel).
_BOOTSTRAP_MODULE_FILE = "_apm_tls_bootstrap.py"
_BOOTSTRAP_PTH_FILE = "_apm_tls.pth"

# Importable name of the bootstrap module (drives the generated .pth line).
_BOOTSTRAP_MODULE_NAME = _BOOTSTRAP_MODULE_FILE.removesuffix(".py")

# Exact content of the generated .pth: a single import line the interpreter runs
# at startup. ASCII, trailing newline. Generated inline so child-trust delivery
# never depends on the .pth being packaged into the wheel.
_PTH_CONTENT = f"import {_BOOTSTRAP_MODULE_NAME}\n"

_TRUTHY = {"1", "true", "yes", "on"}
_LAST_TLS_STATUS: tuple[str, tuple[object, ...]] | None = None
_KNOWN_BUNDLED_CERT_FILE: str | None = None
_MISSING_TLS_REFERENCE = object()

# Retain the unpatched stdlib class for parse validation and certifi fallback.
# ``tls_trust`` is imported before Requests/urllib3 at CLI startup.
_STDLIB_SSL_CONTEXT = ssl.SSLContext


class TLSConfigurationError(RuntimeError):
    """Raised when an explicitly selected TLS trust input is unusable."""


def _record_tls_trust_status(message: str, *args: object) -> None:
    """Cache and emit the selected trust source at debug level."""
    global _LAST_TLS_STATUS
    _LAST_TLS_STATUS = (message, args)
    logger.debug(message, *args)


def log_tls_trust_status() -> None:
    """Re-emit the cached trust source after CLI logging is configured."""
    if _LAST_TLS_STATUS is not None:
        message, args = _LAST_TLS_STATUS
        logger.debug(message, *args)


def _env_flag(name: str, env: Mapping[str, str] | None = None) -> bool:
    environ = os.environ if env is None else env
    return environ.get(name, "").strip().lower() in _TRUTHY


def has_explicit_ca_override(env: Mapping[str, str] | None = None) -> bool:
    """Return True when requests has an explicit CA bundle override."""
    environ = os.environ if env is None else env
    return any((environ.get(var) or "").strip() for var in _EXPLICIT_CA_ENV_VARS)


def explicit_ca_bundle_path(env: Mapping[str, str] | None = None) -> str:
    """Return the Requests-compatible replacement bundle, or an empty string.

    Requests gives ``REQUESTS_CA_BUNDLE`` precedence over
    ``CURL_CA_BUNDLE``. Callers whose hardened Sessions set ``trust_env=False``
    use this helper to retain those replacement semantics explicitly.
    """
    environ = os.environ if env is None else env
    for var in _EXPLICIT_CA_ENV_VARS:
        value = (environ.get(var) or "").strip()
        if value:
            return value
    return ""


def _safe_path_display(path: Path) -> str:
    """Return an ASCII-safe path for CLI diagnostics."""
    return ascii(str(path))[1:-1]


def _read_extra_ca_bundle(env: Mapping[str, str] | None = None) -> tuple[str, str] | None:
    """Read and validate ``APM_EXTRA_CA_BUNDLE`` from one stable file snapshot.

    Returns ``(absolute_path, pem_text)`` when configured and ``None`` for an
    unset/blank value. The opened descriptor must identify a non-empty regular
    file no larger than :data:`_MAX_EXTRA_CA_BUNDLE_BYTES`. The snapshot must
    be printable-ASCII PEM containing at least one certificate. Validation
    uses an unpatched stdlib context, so a prior process-wide TLS injection
    cannot change the parser or recursively add another bundle.

    Raises:
        TLSConfigurationError: If the selected path or PEM is unusable.
    """
    environ = os.environ if env is None else env
    raw_value = (environ.get(_EXTRA_CA_ENV_VAR) or "").strip()
    if not raw_value:
        return None

    try:
        requested = Path(raw_value).expanduser()
        resolved = requested.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        display = _safe_path_display(Path(raw_value))
        raise TLSConfigurationError(f"{_EXTRA_CA_ENV_VAR} path does not exist: {display}") from exc

    display = _safe_path_display(resolved)
    try:
        with resolved.open("rb") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise TLSConfigurationError(
                    f"{_EXTRA_CA_ENV_VAR} must reference a regular file: {display}"
                )
            if metadata.st_size <= 0:
                raise TLSConfigurationError(f"{_EXTRA_CA_ENV_VAR} file is empty: {display}")
            if metadata.st_size > _MAX_EXTRA_CA_BUNDLE_BYTES:
                raise TLSConfigurationError(
                    f"{_EXTRA_CA_ENV_VAR} exceeds the 8 MiB limit: {display}"
                )
            bundle_bytes = handle.read(_MAX_EXTRA_CA_BUNDLE_BYTES + 1)
    except TLSConfigurationError:
        raise
    except OSError as exc:
        raise TLSConfigurationError(f"{_EXTRA_CA_ENV_VAR} file is not readable: {display}") from exc

    if len(bundle_bytes) > _MAX_EXTRA_CA_BUNDLE_BYTES:
        raise TLSConfigurationError(f"{_EXTRA_CA_ENV_VAR} exceeds the 8 MiB limit: {display}")
    try:
        bundle_pem = bundle_bytes.decode("ascii")
    except UnicodeDecodeError as exc:
        raise TLSConfigurationError(
            f"{_EXTRA_CA_ENV_VAR} must contain ASCII PEM certificates: {display}"
        ) from exc
    if any(
        line.startswith("-----BEGIN ") and line.endswith("PRIVATE KEY-----")
        for line in bundle_pem.splitlines()
    ):
        raise TLSConfigurationError(
            f"{_EXTRA_CA_ENV_VAR} must contain certificates only; private keys are not allowed: "
            f"{display}"
        )

    validation_context = _STDLIB_SSL_CONTEXT(ssl.PROTOCOL_TLS_CLIENT)
    try:
        validation_context.load_verify_locations(cadata=bundle_pem)
    except (OSError, ValueError, ssl.SSLError) as exc:
        raise TLSConfigurationError(
            f"{_EXTRA_CA_ENV_VAR} is not a valid PEM certificate bundle: {display}"
        ) from exc
    if validation_context.cert_store_stats().get("x509", 0) < 1:
        raise TLSConfigurationError(f"{_EXTRA_CA_ENV_VAR} contains no certificates: {display}")
    return str(resolved), bundle_pem


def _capture_tls_publication_state() -> tuple[type[ssl.SSLContext], Any, object, Any, object, Any]:
    """Capture every loaded global reference changed by TLS injection.

    This deliberately does not import urllib3 or Requests. The CLI must inject
    truststore before its HTTP stack is first imported; modules loaded during a
    failed injection are normalized by :func:`_restore_tls_publication_state`.
    """
    urllib3_ssl = sys.modules.get("urllib3.util.ssl_")
    requests_adapters = sys.modules.get("requests.adapters")
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
    return (
        ssl.SSLContext,
        urllib3_ssl,
        urllib3_context,
        requests_adapters,
        preloaded,
        ssl._create_default_https_context,
    )


def _restore_tls_publication_state(
    state: tuple[type[ssl.SSLContext], Any, object, Any, object, Any],
) -> None:
    """Restore a state captured before a process-wide TLS publication."""
    (
        original_ssl_context,
        original_urllib3_module,
        original_urllib3_context,
        original_requests_module,
        original_preloaded,
        original_https_factory,
    ) = state
    ssl.SSLContext = original_ssl_context  # type: ignore[misc]
    ssl._create_default_https_context = original_https_factory
    urllib3_ssl = cast(Any, original_urllib3_module or sys.modules.get("urllib3.util.ssl_"))
    if urllib3_ssl is not None:
        urllib3_ssl.SSLContext = (
            original_ssl_context
            if original_urllib3_context is _MISSING_TLS_REFERENCE
            else original_urllib3_context
        )
    requests_adapters = cast(Any, original_requests_module or sys.modules.get("requests.adapters"))
    if requests_adapters is not None:
        if original_requests_module is None:
            if hasattr(requests_adapters, "_preloaded_ssl_context"):
                # Requests 2.32 preloads certifi into a urllib3-created
                # context. Rebuild that exact default after a failed injection;
                # a bare stdlib context would contain no CA roots here.
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
        elif original_preloaded is _MISSING_TLS_REFERENCE:
            with contextlib.suppress(AttributeError):
                del requests_adapters._preloaded_ssl_context
        else:
            requests_adapters._preloaded_ssl_context = original_preloaded


def _install_additive_ca_context(base_context: type[ssl.SSLContext], bundle_pem: str) -> None:
    """Publish an SSLContext type that loads *bundle_pem* on construction.

    Validate the candidate before publishing it. ``configure_tls_trust`` owns
    rollback for the entire injection, including any failure in this helper.
    Replace Requests 2.32's optional preloaded context without mutating it.
    """

    # On fallback, specialize urllib3's context and the stdlib HTTPS factory.
    # Replacing ssl.SSLContext with a stdlib subclass makes its setters recurse.
    # Keeping the
    # setting in-process also avoids exporting new trust to execution children.
    certifi_path = None
    urllib3_ssl = sys.modules.get("urllib3.util.ssl_")
    if base_context is _STDLIB_SSL_CONTEXT:
        import certifi
        import urllib3.util.ssl_ as urllib3_ssl

        certifi_path = certifi.where()

    class _APMExtraCAContext(base_context):  # type: ignore[valid-type,misc]
        def __init__(self, protocol: int | None = None) -> None:
            if certifi_path is None:
                super().__init__(protocol)
            else:
                # stdlib SSLContext initializes in __new__, not __init__.
                self.load_verify_locations(cafile=certifi_path)
            self.load_verify_locations(cadata=bundle_pem)

    # Keep existing trust-source diagnostics meaningful.
    _APMExtraCAContext.__module__ = base_context.__module__

    candidate = _APMExtraCAContext(ssl.PROTOCOL_TLS_CLIENT)
    if not candidate.check_hostname or candidate.verify_mode != ssl.CERT_REQUIRED:
        raise TLSConfigurationError("Additive TLS context did not preserve peer verification")

    if certifi_path is None:
        ssl.SSLContext = _APMExtraCAContext  # type: ignore[misc]
    else:
        # urllib.request/http.client use this factory for metadata HTTPS.
        # Preserve its existing defaults and settings, then add the same PEM.
        original_https_factory = ssl._create_default_https_context

        def _extra_https_context(*args: Any, **kwargs: Any) -> ssl.SSLContext:
            context = original_https_factory(*args, **kwargs)
            context.load_verify_locations(cadata=bundle_pem)
            return context

        https_candidate = _extra_https_context()
        if not https_candidate.check_hostname or https_candidate.verify_mode != ssl.CERT_REQUIRED:
            raise TLSConfigurationError("Additive HTTPS context did not preserve peer verification")
        ssl._create_default_https_context = _extra_https_context
    if urllib3_ssl is not None:
        urllib3_ssl.SSLContext = _APMExtraCAContext
    requests_adapters = sys.modules.get("requests.adapters")
    if requests_adapters is not None and hasattr(requests_adapters, "_preloaded_ssl_context"):
        requests_adapters._preloaded_ssl_context = candidate


def _mutable_environ(env: Mapping[str, str] | None) -> MutableMapping[str, str]:
    """Return the environment truststore/OpenSSL will actually read.

    ``env is None`` is the real runtime path -- operate on ``os.environ`` so the
    pop/restore of SSL_CERT_FILE takes effect before OpenSSL reads it. When an
    explicit mapping is passed (tests), operate on it if mutable, else fall back
    to ``os.environ`` to match the read semantics of the other helpers.
    """
    if env is None:
        return os.environ
    if isinstance(env, MutableMapping):
        return env
    return os.environ


def configure_tls_trust(env: Mapping[str, str] | None = None) -> bool:
    """Route HTTPS verification through the OS trust store when possible.

    Call once at process startup, before the first HTTPS request. Returns
    ``True`` when ``truststore`` was injected, ``False`` when the default
    ``certifi`` behaviour was retained (explicit override, opt-out,
    ``truststore`` missing, or injection failure). When
    ``APM_EXTRA_CA_BUNDLE`` is selected, its validated PEM is added to the OS
    context or to the certifi fallback without changing replacement-variable
    semantics.

    Raises:
        TLSConfigurationError: If an explicitly selected additive bundle is
            missing, unreadable, non-regular, oversized, empty, or malformed.
    """
    global _KNOWN_BUNDLED_CERT_FILE
    environ = _mutable_environ(env)

    # Clear the bundled-default marker unconditionally, up-front, so it can
    # never leak into child processes on ANY return path (opt-out, explicit
    # override, truststore-import failure, inject success, or inject failure).
    # Capture its truthiness first -- the pop-before-inject logic below needs
    # to know whether the current SSL_CERT_FILE was OUR bundled default.
    had_bundled_marker = _env_flag(_BUNDLED_CERT_MARKER, environ)
    environ.pop(_BUNDLED_CERT_MARKER, None)
    if had_bundled_marker and environ.get(_SSL_CERT_FILE_VAR):
        _KNOWN_BUNDLED_CERT_FILE = os.path.abspath(environ[_SSL_CERT_FILE_VAR])

    if has_explicit_ca_override(environ):
        explicit_path = explicit_ca_bundle_path(environ)
        _record_tls_trust_status(
            "TLS: explicit CA bundle in use: %s", _safe_path_display(Path(explicit_path))
        )
        return False

    if _env_flag(_DISABLE_ENV_VAR, environ):
        _record_tls_trust_status("TLS: OS trust-store injection disabled (%s)", _DISABLE_ENV_VAR)
        return False

    extra_ca = _read_extra_ca_bundle(environ)
    extra_ca_path = extra_ca[0] if extra_ca is not None else ""
    extra_ca_pem = extra_ca[1] if extra_ca is not None else ""
    extra_ca_display = _safe_path_display(Path(extra_ca_path)) if extra_ca_path else ""

    bundled_cert: str | None = None
    publication_state = _capture_tls_publication_state()
    try:
        # Import and injection failures share the same verified fallback.
        import truststore

        # Remove only the frozen hook's certifi default so truststore can read
        # the OS roots. Preserve a user-selected SSL_CERT_FILE.
        if environ.get(_SSL_CERT_FILE_VAR) and had_bundled_marker:
            bundled_cert = environ.pop(_SSL_CERT_FILE_VAR)

        truststore.inject_into_ssl()
        if extra_ca is not None:
            _install_additive_ca_context(truststore.SSLContext, extra_ca_pem)
    except Exception as exc:
        # Injection changes process-wide globals and can fail after publishing
        # only some of them. Restore the exact pre-injection state before
        # selecting a fallback so no caller observes a mixed trust mode.
        _restore_tls_publication_state(publication_state)
        if bundled_cert is not None:
            environ[_SSL_CERT_FILE_VAR] = bundled_cert
        if extra_ca is not None:
            try:
                _install_additive_ca_context(_STDLIB_SSL_CONTEXT, extra_ca_pem)
            except Exception as fallback_exc:
                _restore_tls_publication_state(publication_state)
                raise TLSConfigurationError(
                    "Could not apply APM_EXTRA_CA_BUNDLE to the HTTPS client"
                ) from fallback_exc
            _record_tls_trust_status(
                "TLS: verifying against bundled CA plus additive CA: %s (certifi fallback) [%s]",
                extra_ca_display,
                exc,
            )
            return False
        _record_tls_trust_status("TLS: verifying against bundled CA (certifi fallback) [%s]", exc)
        return False

    if extra_ca is not None:
        _record_tls_trust_status(
            "TLS: verifying against OS trust store plus additive CA: %s", extra_ca_display
        )
    else:
        _record_tls_trust_status("TLS: verifying against OS trust store (truststore)")
    return True


@functools.lru_cache(maxsize=1)
def configure_process_tls_trust() -> bool:
    """Configure process TLS once while retaining the selected trust source."""
    return configure_tls_trust()


def _child_bootstrap_dir() -> str | None:
    """Absolute path to the directory holding the child TLS bootstrap files.

    Resolves for both source-installed and frozen (PyInstaller) layouts. Returns
    ``None`` if the path cannot be determined -- callers degrade gracefully.
    """
    try:
        if getattr(sys, "frozen", False):
            base = getattr(sys, "_MEIPASS", None)
            if not base:
                return None
            candidate = Path(base) / "apm_cli" / "core" / _CHILD_SHIM_DIRNAME
        else:
            candidate = Path(__file__).resolve().parent / _CHILD_SHIM_DIRNAME
        return str(candidate)
    except Exception:
        return None


def _venv_site_packages(venv_path: Path) -> Path | None:
    """Return the site-packages dir of *venv_path*, or ``None`` if not found.

    Handles the POSIX ``lib/pythonX.Y/site-packages`` and the Windows
    ``Lib/site-packages`` layouts. Picks the first existing match.
    """
    candidates = list(venv_path.glob("lib/python*/site-packages"))
    candidates.extend(venv_path.glob("Lib/site-packages"))
    for candidate in candidates:
        if candidate.is_dir():
            try:
                return ensure_path_within(candidate, venv_path)
            except (OSError, PathTraversalError):
                continue
    return None


def _atomic_write(target: Path, data: bytes) -> None:
    """Write *data* to *target* atomically via a same-dir temp file + os.replace.

    A same-directory temp file guarantees ``os.replace`` performs a rename
    (atomic on POSIX and NTFS), so a reader never observes a truncated file
    under a live ``.pth``. On any OSError the temp file is removed and the error
    re-raised so the caller can report failure without leaving a partial file.
    """
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".apm_tls_", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_name, target)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def ensure_child_tls_bootstrap(venv_path: str | os.PathLike[str]) -> bool:
    """Install the self-contained OS-trust bootstrap into a child venv.

    Writes ``_apm_tls_bootstrap.py`` (the shipped module) and a generated
    ``_apm_tls.pth`` into the venv's site-packages so its interpreter injects
    ``truststore`` at startup -- no ``apm_cli`` dependency, no ``PYTHONPATH``
    mutation. Python-driven (rather than shell-globbed) so it resolves the
    shipped module identically for a source install (package dir) and a frozen
    binary (``sys._MEIPASS``).

    The ``.pth`` content is GENERATED inline rather than copied: setuptools'
    ``packages.find`` omits stray ``.pth`` data files from the wheel, so copying
    it would silently no-op on the PyPI channel. Delivery therefore depends only
    on the module data file, which is packaged.

    Both files are written atomically, module FIRST, so the ``.pth`` is never
    present without a complete bootstrap module behind it (avoiding a truncated
    module under a live ``.pth`` -> per-invocation stderr traceback storm).

    Idempotent and best-effort: returns ``True`` when both files are present
    after the call, ``False`` on any failure (no partial file left). Never
    raises.
    """
    try:
        site_packages = _venv_site_packages(Path(venv_path))
        if site_packages is None:
            return False
        source_dir = _child_bootstrap_dir()
        if not source_dir:
            return False
        module_src = Path(source_dir) / _BOOTSTRAP_MODULE_FILE
        if not module_src.is_file():
            return False
        # Module first (atomic), then the generated .pth (atomic) -- the write
        # order guarantees the .pth never activates an incomplete module.
        _atomic_write(site_packages / _BOOTSTRAP_MODULE_FILE, module_src.read_bytes())
        _atomic_write(site_packages / _BOOTSTRAP_PTH_FILE, _PTH_CONTENT.encode("ascii"))
        return True
    except Exception:
        return False


def _is_bundled_certifi(path: str) -> bool:
    """Return True when *path* is APM's bundled certifi CA set (not a user value).

    The frozen runtime hook (build/hooks/runtime_hook_ssl_certs.py) sets
    ``SSL_CERT_FILE`` to ``certifi.where()``. A genuine user-set ``SSL_CERT_FILE``
    must NEVER match. Compare exact normalized paths against the path captured
    from the internal marker and against ``certifi.where()``.
    """
    if not path:
        return False
    normalized = os.path.normcase(os.path.abspath(path))
    if _KNOWN_BUNDLED_CERT_FILE and normalized == os.path.normcase(
        os.path.abspath(_KNOWN_BUNDLED_CERT_FILE)
    ):
        return True
    try:
        import certifi

        return normalized == os.path.normcase(os.path.abspath(certifi.where()))
    except Exception:
        return False


def build_child_tls_env(base_env: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of *base_env* scrubbed for a spawned child runtime.

    Trust is now delivered at venv-setup time via the ``.pth`` bootstrap
    (:func:`ensure_child_tls_bootstrap`), NOT at spawn time via ``PYTHONPATH``
    -- prepending a shim dir would shadow a user/corporate ``sitecustomize.py``
    and only reached children that shared this process's ``sys.path``.

    This function is an env-hygiene pass:

    * strips the internal bundled-default marker so the frozen binary's
      ``SSL_CERT_FILE`` marker never leaks into a child, and
    * drops ``SSL_CERT_FILE`` WHEN it points at the bundled certifi set, so the
      child's ``truststore`` reaches the OS store on Linux (where truststore
      honours ``SSL_CERT_FILE``). A frozen parent whose injection failed
      restores ``SSL_CERT_FILE=certifi`` and would otherwise leak it, pinning
      the child to certifi instead of the OS store. A GENUINE user
      ``SSL_CERT_FILE`` is preserved -- only the bundled default is dropped.
    """
    child = dict(base_env)
    child.pop(_BUNDLED_CERT_MARKER, None)
    cert_file = child.get(_SSL_CERT_FILE_VAR)
    if cert_file and _is_bundled_certifi(cert_file):
        child.pop(_SSL_CERT_FILE_VAR, None)
    return child
