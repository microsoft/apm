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

import logging
import os
import shutil
import sys
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

# The two artifacts copied into a child venv's site-packages to deliver
# OS-trust at interpreter startup (see ensure_child_tls_bootstrap).
_BOOTSTRAP_MODULE_FILE = "_apm_tls_bootstrap.py"
_BOOTSTRAP_PTH_FILE = "_apm_tls.pth"

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


def ensure_child_tls_bootstrap(venv_path: str | os.PathLike[str]) -> bool:
    """Install the self-contained OS-trust bootstrap into a child venv.

    Copies ``_apm_tls_bootstrap.py`` and ``_apm_tls.pth`` into the venv's
    site-packages so its interpreter injects ``truststore`` at startup -- no
    ``apm_cli`` dependency, no ``PYTHONPATH`` mutation. Python-driven (rather
    than shell-globbed) so it resolves the shipped source identically for a
    source install (package dir) and a frozen binary (``sys._MEIPASS``).

    Idempotent and best-effort: returns ``True`` when both files are present
    after the call, ``False`` on any failure. Never raises.
    """
    try:
        site_packages = _venv_site_packages(Path(venv_path))
        if site_packages is None:
            return False
        source_dir = _child_bootstrap_dir()
        if not source_dir:
            return False
        source = Path(source_dir)
        for name in (_BOOTSTRAP_MODULE_FILE, _BOOTSTRAP_PTH_FILE):
            src_file = source / name
            if not src_file.is_file():
                return False
            shutil.copyfile(src_file, site_packages / name)
        return True
    except Exception:
        return False


def build_child_tls_env(base_env: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of *base_env* scrubbed for a spawned child runtime.

    Trust is now delivered at venv-setup time via the ``.pth`` bootstrap
    (:func:`ensure_child_tls_bootstrap`), NOT at spawn time via ``PYTHONPATH``
    -- prepending a shim dir would shadow a user/corporate ``sitecustomize.py``
    and only reached children that shared this process's ``sys.path``.

    This function is now an env-hygiene pass: it strips the internal
    bundled-default marker so the frozen binary's ``SSL_CERT_FILE`` marker never
    leaks into a child, then returns the env unchanged otherwise.
    """
    child = dict(base_env)
    child.pop(_BUNDLED_CERT_MARKER, None)
    return child
