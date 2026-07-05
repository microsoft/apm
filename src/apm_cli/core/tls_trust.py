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

Child runtimes (``llm``, ``codex``, ``copilot``, ...) are Python/CLI
subprocesses spawned after ``exec``; the parent cannot monkeypatch their
``ssl`` module. ``build_child_tls_env`` propagates trust by prepending a
``sitecustomize`` shim directory to the child ``PYTHONPATH`` so each Python
child re-runs :func:`configure_tls_trust` at its own interpreter startup.
"""

from __future__ import annotations

import logging
import os
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

# Directory (relative to the installed package) that holds the child shim.
_CHILD_SHIM_DIRNAME = "_child_tls"

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

    environ = _mutable_environ(env)

    # If the frozen hook pinned SSL_CERT_FILE to bundled certifi, pop it so
    # truststore's set_default_verify_paths() reads the genuine system default.
    # A user-set SSL_CERT_FILE (no marker) is left untouched.
    bundled_cert: str | None = None
    if environ.get(_SSL_CERT_FILE_VAR) and _env_flag(_BUNDLED_CERT_MARKER, env):
        bundled_cert = environ.get(_SSL_CERT_FILE_VAR)
        environ.pop(_SSL_CERT_FILE_VAR, None)

    try:
        truststore.inject_into_ssl()
    except Exception as exc:
        # Never end with zero trust: restore the bundled certifi path so
        # musl/minimal-container hosts still verify against certifi.
        if bundled_cert is not None:
            environ[_SSL_CERT_FILE_VAR] = bundled_cert
        environ.pop(_BUNDLED_CERT_MARKER, None)
        logger.debug("[i] TLS: verifying against bundled CA (certifi fallback) [%s]", exc)
        return False

    # Clear the marker so it does not leak into child processes.
    environ.pop(_BUNDLED_CERT_MARKER, None)
    logger.debug("[i] TLS: verifying against OS trust store (truststore)")
    return True


def _child_shim_dir() -> str | None:
    """Absolute path to the directory containing the child ``sitecustomize``.

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


def build_child_tls_env(base_env: Mapping[str, str]) -> dict[str, str]:
    """Return a child env that re-runs the trust bootstrap at its startup.

    Prepends the ``sitecustomize`` shim directory to ``PYTHONPATH`` (preserving
    any existing value) so each Python child imports the shim and re-invokes
    :func:`configure_tls_trust` -- single source of truth, no logic duplicated.

    * ``APM_DISABLE_TRUSTSTORE`` truthy: return the env unchanged (no shim).
    * An explicit CA override still gets the shim; the shim's own
      ``configure_tls_trust`` declines to inject and leaves the bundle intact.
    """
    child = dict(base_env)

    if _env_flag(_DISABLE_ENV_VAR, base_env):
        return child

    shim_dir = _child_shim_dir()
    if not shim_dir:
        return child

    existing = child.get("PYTHONPATH", "")
    child["PYTHONPATH"] = shim_dir + os.pathsep + existing if existing else shim_dir
    return child
