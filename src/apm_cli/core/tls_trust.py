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

Child runtimes are Python/CLI subprocesses spawned after ``exec``; the parent
cannot monkeypatch their ``ssl`` module. Trust is delivered to the Python
``llm`` runtime at venv-setup time: :func:`ensure_child_tls_bootstrap` drops a
self-contained ``.pth`` bootstrap into the runtime venv's site-packages so its
interpreter injects ``truststore`` at startup with no ``apm_cli`` dependency and
no ``PYTHONPATH`` mutation (which would shadow a user ``sitecustomize.py``).
:func:`build_child_tls_env` is now an env-hygiene pass only. Node (Copilot) and
Rust (Codex) runtimes verify against their own default trust for now (#2034).
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import tempfile
from collections.abc import Mapping, MutableMapping
from pathlib import Path

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


def _env_flag(name: str, env: Mapping[str, str] | None = None) -> bool:
    environ = os.environ if env is None else env
    return environ.get(name, "").strip().lower() in _TRUTHY


def has_explicit_ca_override(env: Mapping[str, str] | None = None) -> bool:
    """Return True when requests has an explicit CA bundle override."""
    environ = os.environ if env is None else env
    return any((environ.get(var) or "").strip() for var in _EXPLICIT_CA_ENV_VARS)


def _explicit_ca_path(env: Mapping[str, str] | None = None) -> str:
    """Return the first explicit CA-bundle path set, or an empty string."""
    environ = os.environ if env is None else env
    for var in _EXPLICIT_CA_ENV_VARS:
        value = (environ.get(var) or "").strip()
        if value:
            return value
    return ""


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
    ``certifi`` behaviour was left in place (explicit override, opt-out,
    ``truststore`` missing, or injection failure). Never raises.
    """
    environ = _mutable_environ(env)

    # Clear the bundled-default marker unconditionally, up-front, so it can
    # never leak into child processes on ANY return path (opt-out, explicit
    # override, truststore-import failure, inject success, or inject failure).
    # Capture its truthiness first -- the pop-before-inject logic below needs
    # to know whether the current SSL_CERT_FILE was OUR bundled default.
    had_bundled_marker = _env_flag(_BUNDLED_CERT_MARKER, env)
    environ.pop(_BUNDLED_CERT_MARKER, None)

    if _env_flag(_DISABLE_ENV_VAR, env):
        logger.debug("[i] TLS: OS trust-store injection disabled (%s)", _DISABLE_ENV_VAR)
        return False

    if has_explicit_ca_override(env):
        logger.debug("[i] TLS: explicit CA bundle in use: %s", _explicit_ca_path(env))
        return False

    try:
        # Broad except: a broken/incompatible install can fail at import, not
        # only with ImportError -- degrade instead of crashing startup.
        import truststore
    except Exception as exc:
        logger.debug("[i] TLS: verifying against bundled CA (certifi fallback) [%s]", exc)
        return False

    # If the frozen hook pinned SSL_CERT_FILE to bundled certifi, pop it so
    # truststore's set_default_verify_paths() reads the genuine system default.
    # A user-set SSL_CERT_FILE (no marker) is left untouched.
    bundled_cert: str | None = None
    if environ.get(_SSL_CERT_FILE_VAR) and had_bundled_marker:
        bundled_cert = environ.get(_SSL_CERT_FILE_VAR)
        environ.pop(_SSL_CERT_FILE_VAR, None)

    try:
        truststore.inject_into_ssl()
    except Exception as exc:
        # Never end with zero trust: restore the bundled certifi path so
        # musl/minimal-container hosts still verify against certifi.
        if bundled_cert is not None:
            environ[_SSL_CERT_FILE_VAR] = bundled_cert
        logger.debug("[i] TLS: verifying against bundled CA (certifi fallback) [%s]", exc)
        return False

    logger.debug("[i] TLS: verifying against OS trust store (truststore)")
    return True


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
            return candidate
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
    must NEVER match. We compare against ``certifi.where()`` and, as a
    frozen-safe fallback, the ``certifi/cacert.pem`` path tail (the frozen
    ``_MEIPASS`` path may differ from the live dev ``certifi.where()``).
    """
    if not path:
        return False
    if path.replace("\\", "/").endswith("certifi/cacert.pem"):
        return True
    try:
        import certifi

        return os.path.abspath(path) == os.path.abspath(certifi.where())
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
