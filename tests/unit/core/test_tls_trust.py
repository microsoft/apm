"""Unit tests for apm_cli.core.tls_trust.configure_tls_trust.

Covers every branch:
- opt-out via APM_DISABLE_TRUSTSTORE
- explicit CA bundle env vars win (REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE)
- SSL_CERT_FILE / SSL_CERT_DIR do NOT suppress injection
- truststore missing -> graceful certifi fallback
- injection failure -> graceful certifi fallback
- happy path -> inject_into_ssl called exactly once
"""

from __future__ import annotations

import ast
import logging
import os
import ssl
import subprocess
import sys
import types
from pathlib import Path

import pytest

from apm_cli.core.tls_trust import (
    _BUNDLED_CERT_MARKER,
    _DISABLE_ENV_VAR,
    _EXPLICIT_CA_ENV_VARS,
    _EXTRA_CA_ENV_VAR,
    _MAX_EXTRA_CA_BUNDLE_BYTES,
    TLSConfigurationError,
    build_child_tls_env,
    configure_tls_trust,
    ensure_child_tls_bootstrap,
    has_explicit_ca_override,
    log_tls_trust_status,
    selected_tls_trust,
    tls_failure_guidance,
)

_NON_REQUESTS_CA_ENV_VARS = ("SSL_CERT_FILE", "SSL_CERT_DIR")
_ALL_TRUST_ENV = (
    _DISABLE_ENV_VAR,
    *_NON_REQUESTS_CA_ENV_VARS,
    *_EXPLICIT_CA_ENV_VARS,
    _EXTRA_CA_ENV_VAR,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Start each test from a pristine env (no override / opt-out set).

    Also restore the module's recorded trust selection afterwards so a
    synthetic selection made here never leaks into failure-guidance tests.
    """
    import apm_cli.core.tls_trust as tls

    for var in _ALL_TRUST_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(tls, "_LAST_TLS_STATUS", tls._LAST_TLS_STATUS)
    monkeypatch.setattr(tls, "_LAST_TLS_SELECTION", tls._LAST_TLS_SELECTION)


def _install_fake_truststore(monkeypatch, inject=None, ssl_context=None):
    """Put a fake ``truststore`` module in sys.modules and return its inject mock."""
    calls = {"n": 0}

    def _default_inject():
        calls["n"] += 1

    module = types.ModuleType("truststore")
    module.inject_into_ssl = inject or _default_inject  # type: ignore[attr-defined]
    module.SSLContext = ssl_context or object  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "truststore", module)
    return calls


def test_opt_out_disables_injection(monkeypatch):
    calls = _install_fake_truststore(monkeypatch)

    assert configure_tls_trust(env={_DISABLE_ENV_VAR: "1"}) is False
    assert calls["n"] == 0


@pytest.mark.parametrize("var", _EXPLICIT_CA_ENV_VARS)
def test_explicit_ca_bundle_wins(monkeypatch, var):
    calls = _install_fake_truststore(monkeypatch)

    assert has_explicit_ca_override(env={var: "/etc/ssl/certs/custom-ca.pem"}) is True
    assert configure_tls_trust(env={var: "/etc/ssl/certs/custom-ca.pem"}) is False
    assert calls["n"] == 0


@pytest.mark.parametrize("var", _NON_REQUESTS_CA_ENV_VARS)
def test_non_requests_ca_env_does_not_suppress_injection(monkeypatch, var):
    # SSL_CERT_FILE and SSL_CERT_DIR are not requests CA overrides. The frozen
    # runtime hook sets SSL_CERT_FILE to bundled certifi, so these vars must not
    # disable OS-trust injection in the shipped artifact.
    calls = _install_fake_truststore(monkeypatch)
    env = {var: "/etc/ssl/certs/ca-certificates.crt"}

    assert has_explicit_ca_override(env=env) is False
    assert configure_tls_trust(env=env) is True
    assert calls["n"] == 1


def test_missing_truststore_falls_back(monkeypatch):
    # A None entry in sys.modules makes ``import truststore`` raise ImportError.
    monkeypatch.setitem(sys.modules, "truststore", None)

    assert configure_tls_trust() is False


def test_injection_failure_falls_back(monkeypatch):
    def _boom():
        raise RuntimeError("platform trust API unavailable")

    _install_fake_truststore(monkeypatch, inject=_boom)

    assert configure_tls_trust() is False


def test_post_injection_additive_failure_rolls_back_all_globals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A late additive failure cannot leave a partially injected process."""
    import certifi
    import requests.adapters
    import urllib3.util.ssl_ as urllib3_ssl

    import apm_cli.core.tls_trust as tls

    original_ssl = ssl.SSLContext
    original_https_factory = ssl._create_default_https_context
    original_urllib3 = urllib3_ssl.SSLContext
    preloaded_was_present = hasattr(requests.adapters, "_preloaded_ssl_context")
    original_preloaded = getattr(requests.adapters, "_preloaded_ssl_context", None)

    class PartiallyPublishedContext:
        pass

    module = types.ModuleType("truststore")
    module.SSLContext = PartiallyPublishedContext  # type: ignore[attr-defined]

    def _partial_inject() -> None:
        ssl.SSLContext = PartiallyPublishedContext  # type: ignore[misc]
        ssl._create_default_https_context = lambda: object()
        urllib3_ssl.SSLContext = PartiallyPublishedContext  # type: ignore[assignment]
        requests.adapters._preloaded_ssl_context = object()

    module.inject_into_ssl = _partial_inject  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "truststore", module)

    def _late_failure(_base_context: type, _bundle_pem: str) -> None:
        raise TLSConfigurationError("forced post-injection failure")

    monkeypatch.setattr(tls, "_install_additive_ca_context", _late_failure)
    env = {_EXTRA_CA_ENV_VAR: certifi.where()}

    with pytest.raises(TLSConfigurationError, match="Could not apply"):
        configure_tls_trust(env=env)
    assert ssl.SSLContext is original_ssl
    assert ssl._create_default_https_context is original_https_factory
    assert urllib3_ssl.SSLContext is original_urllib3
    assert hasattr(requests.adapters, "_preloaded_ssl_context") is preloaded_was_present
    assert getattr(requests.adapters, "_preloaded_ssl_context", None) is original_preloaded
    assert env == {_EXTRA_CA_ENV_VAR: certifi.where()}


def test_failed_injection_rebuilds_new_requests_232_preloaded_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Requests module first imported mid-injection retains its certifi roots."""
    fake_adapters = types.ModuleType("requests.adapters")

    class RestoredContext:
        def __init__(self) -> None:
            self.loaded_paths: list[str] = []

        def load_verify_locations(self, path: str) -> None:
            self.loaded_paths.append(path)

    fake_adapters._preloaded_ssl_context = object()  # type: ignore[attr-defined]
    fake_adapters.DEFAULT_CA_BUNDLE_PATH = "/bundled/certifi.pem"  # type: ignore[attr-defined]
    fake_adapters.extract_zipped_paths = lambda path: path  # type: ignore[attr-defined]
    fake_adapters.create_urllib3_context = RestoredContext  # type: ignore[attr-defined]

    monkeypatch.delitem(sys.modules, "requests.adapters", raising=False)

    module = types.ModuleType("truststore")
    module.SSLContext = object  # type: ignore[attr-defined]

    def _partial_inject_then_fail() -> None:
        sys.modules["requests.adapters"] = fake_adapters
        raise RuntimeError("forced partial injection")

    module.inject_into_ssl = _partial_inject_then_fail  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "truststore", module)

    assert configure_tls_trust() is False
    restored = fake_adapters._preloaded_ssl_context  # type: ignore[attr-defined]
    assert isinstance(restored, RestoredContext)
    assert restored.loaded_paths == ["/bundled/certifi.pem"]


def test_happy_path_injects_once(monkeypatch):
    calls = _install_fake_truststore(monkeypatch)

    assert configure_tls_trust() is True
    assert calls["n"] == 1


def test_valid_additive_bundle_extends_injected_parent_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid extra bundle is applied on top of the injected OS context."""
    import certifi

    import apm_cli.core.tls_trust as tls

    class FakeOSContext:
        pass

    calls = _install_fake_truststore(monkeypatch, ssl_context=FakeOSContext)
    installed: dict[str, object] = {}

    def _capture_install(base_context: type, bundle_pem: str) -> None:
        installed["base_context"] = base_context
        installed["bundle_pem"] = bundle_pem

    monkeypatch.setattr(tls, "_install_additive_ca_context", _capture_install)

    assert configure_tls_trust(env={_EXTRA_CA_ENV_VAR: certifi.where()}) is True
    assert calls["n"] == 1
    assert installed["base_context"] is FakeOSContext
    assert "-----BEGIN CERTIFICATE-----" in str(installed["bundle_pem"])


@pytest.mark.parametrize(
    "precedence",
    [
        {_DISABLE_ENV_VAR: "1"},
        {"REQUESTS_CA_BUNDLE": "/replacement/requests.pem"},
        {"CURL_CA_BUNDLE": "/replacement/curl.pem"},
    ],
    ids=["disabled", "requests", "curl"],
)
def test_higher_precedence_controls_skip_invalid_additive_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    precedence: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Disable/replacement settings win before extra-path validation or mapping."""
    missing = tmp_path / "must-not-be-read.pem"
    env = {_EXTRA_CA_ENV_VAR: str(missing), **precedence}
    calls = _install_fake_truststore(monkeypatch)

    assert configure_tls_trust(env=env) is False
    assert calls["n"] == 0
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="apm_cli.core.tls_trust"):
        log_tls_trust_status()
    messages = _trust_source_messages(caplog)
    assert len(messages) == 1
    assert "APM_EXTRA_CA_BUNDLE is ignored by this higher-precedence setting" in messages[0]


def test_explicit_requests_bundle_remains_authoritative_with_disable(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The opt-out suppresses injection; it never unsets a replacement bundle."""
    replacement = str(tmp_path / "replacement.pem")
    env = {
        _DISABLE_ENV_VAR: "1",
        "REQUESTS_CA_BUNDLE": replacement,
        _EXTRA_CA_ENV_VAR: str(tmp_path / "must-not-be-read.pem"),
    }

    with caplog.at_level(logging.DEBUG, logger="apm_cli.core.tls_trust"):
        assert configure_tls_trust(env=env) is False

    assert any("explicit CA bundle in use" in message for message in _trust_source_messages(caplog))


def _invalid_extra_ca_path(tmp_path: Path, case: str) -> Path:
    candidate = tmp_path / f"{case}.pem"
    if case == "missing":
        return candidate
    if case == "empty":
        candidate.touch()
    elif case == "directory":
        candidate.mkdir()
    elif case == "malformed":
        candidate.write_text("this is not a PEM certificate\n", encoding="ascii")
    elif case == "non-ascii":
        candidate.write_bytes(b"\xff\xfe\xfd")
    elif case == "oversized":
        # Seek makes this sparse where supported; only the bounded-size check
        # matters, so the test need not allocate an 8 MiB in-memory payload.
        with candidate.open("wb") as handle:
            handle.seek(_MAX_EXTRA_CA_BUNDLE_BYTES)
            handle.write(b"x")
    elif case == "private-key":
        import certifi

        private_key_label = b"PRIVATE " + b"KEY"
        candidate.write_bytes(
            Path(certifi.where()).read_bytes()
            + b"\n-----BEGIN "
            + private_key_label
            + b"-----\nAA==\n-----END "
            + private_key_label
            + b"-----\n"
        )
    else:  # pragma: no cover - parametrization is the closed set
        raise AssertionError(case)
    return candidate


@pytest.mark.parametrize(
    "case",
    ["missing", "empty", "directory", "malformed", "non-ascii", "oversized", "private-key"],
)
def test_invalid_additive_bundle_fails_before_injection(tmp_path: Path, case: str) -> None:
    selected = _invalid_extra_ca_path(tmp_path, case)
    env = {_EXTRA_CA_ENV_VAR: str(selected)}

    with pytest.raises(TLSConfigurationError):
        configure_tls_trust(env=env)


@pytest.mark.windows_compat
def test_internal_tls_failure_names_setting_in_cli_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    from click.testing import CliRunner

    cli_module = importlib.import_module("apm_cli.cli")
    monkeypatch.setattr(
        cli_module,
        "_TLS_BOOTSTRAP_ERROR",
        TLSConfigurationError("Additive TLS context did not preserve peer verification"),
    )
    result = CliRunner().invoke(cli_module.cli, ["install"])
    assert result.exit_code == 1
    assert "Check the APM_EXTRA_CA_BUNDLE setting, or unset it and retry" in result.output


def _repo_root() -> Path:
    current = Path(__file__).resolve().parent
    for parent in (current, *current.parents):
        if (parent / "pyproject.toml").is_file():
            return parent
    raise RuntimeError("Cannot locate repository root")


def test_cli_bootstrap_injects_before_requests_import(tmp_path):
    sentinel = tmp_path / "sentinel.txt"
    fake_truststore = tmp_path / "truststore.py"
    fake_truststore.write_text(
        "\n".join(
            [
                "import os",
                "import pathlib",
                "import sys",
                "",
                "def inject_into_ssl():",
                "    pathlib.Path(os.environ['TRUSTSTORE_SENTINEL']).write_text(",
                "        'requests_imported=' + str('requests' in sys.modules),",
                "        encoding='utf-8',",
                "    )",
            ]
        ),
        encoding="utf-8",
    )

    env = os.environ.copy()
    for name in _ALL_TRUST_ENV:
        env.pop(name, None)
    env["PYTHONPATH"] = f"{tmp_path}{os.pathsep}{_repo_root() / 'src'}"
    env["TRUSTSTORE_SENTINEL"] = str(sentinel)

    result = subprocess.run(
        [sys.executable, "-c", "import apm_cli.cli"],
        cwd=_repo_root(),
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert sentinel.read_text(encoding="utf-8") == "requests_imported=False"


def test_cli_bootstrap_is_idempotent_across_import_and_main(tmp_path):
    sentinel = tmp_path / "sentinel.txt"
    fake_truststore = tmp_path / "truststore.py"
    fake_truststore.write_text(
        "\n".join(
            [
                "import os",
                "import pathlib",
                "",
                "def inject_into_ssl():",
                "    path = pathlib.Path(os.environ['TRUSTSTORE_SENTINEL'])",
                "    count = int(path.read_text(encoding='utf-8') or '0') if path.exists() else 0",
                "    path.write_text(str(count + 1), encoding='utf-8')",
            ]
        ),
        encoding="utf-8",
    )

    env = os.environ.copy()
    for name in _ALL_TRUST_ENV:
        env.pop(name, None)
    env["PYTHONPATH"] = f"{tmp_path}{os.pathsep}{_repo_root() / 'src'}"
    env["TRUSTSTORE_SENTINEL"] = str(sentinel)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import apm_cli.cli as c\n"
            "def fake_cli(*, obj):\n"
            "    return None\n"
            "c.cli = fake_cli\n"
            "c.main()\n",
        ],
        cwd=_repo_root(),
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert sentinel.read_text(encoding="utf-8") == "1"


# ---------------------------------------------------------------------------
# H2 -- visible trust-source diagnostic. Each branch of configure_tls_trust must
# emit an ASCII "TLS: ..." line at DEBUG naming which trust source is in
# effect, so an operator can tell OS-store vs certifi-fallback vs explicit
# bundle vs opt-out from the logs alone.
# ---------------------------------------------------------------------------


def _trust_source_messages(caplog):
    """Rendered log messages emitted by configure_tls_trust that name a trust source."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "apm_cli.core.tls_trust" and "TLS:" in record.getMessage()
    ]


def test_diag_default_inject_names_os_trust_store(monkeypatch, caplog):
    _install_fake_truststore(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger="apm_cli.core.tls_trust"):
        assert configure_tls_trust() is True

    messages = _trust_source_messages(caplog)
    assert "TLS: verifying against OS trust store (truststore)" in messages
    for message in messages:
        message.encode("ascii")  # must not raise


def test_diag_disabled_names_opt_out(caplog):
    with caplog.at_level(logging.DEBUG, logger="apm_cli.core.tls_trust"):
        assert configure_tls_trust(env={_DISABLE_ENV_VAR: "1"}) is False

    messages = _trust_source_messages(caplog)
    assert "TLS: OS trust-store injection disabled (APM_DISABLE_TRUSTSTORE)" in messages
    for message in messages:
        message.encode("ascii")


def test_diag_explicit_bundle_names_the_path(tmp_path, caplog):
    ca_path = str(tmp_path / "corp-root.pem")
    with caplog.at_level(logging.DEBUG, logger="apm_cli.core.tls_trust"):
        assert configure_tls_trust(env={"REQUESTS_CA_BUNDLE": ca_path}) is False

    messages = _trust_source_messages(caplog)
    display = ascii(str(Path(ca_path)))[1:-1]
    assert f"TLS: explicit CA bundle in use: {display}" in messages
    for message in messages:
        message.encode("ascii")


def test_diag_import_failure_names_certifi_fallback(monkeypatch, caplog):
    # A None entry makes ``import truststore`` raise -> certifi-fallback branch.
    monkeypatch.setitem(sys.modules, "truststore", None)
    with caplog.at_level(logging.DEBUG, logger="apm_cli.core.tls_trust"):
        assert configure_tls_trust() is False

    messages = _trust_source_messages(caplog)
    # The branch appends the captured exception in brackets; match the stable core.
    assert any(
        m.startswith("TLS: verifying against bundled CA (certifi fallback)") for m in messages
    ), messages
    for message in messages:
        message.encode("ascii")


def test_cached_trust_source_can_be_replayed_after_logging_configuration(monkeypatch, caplog):
    _install_fake_truststore(monkeypatch)
    configure_tls_trust()
    caplog.clear()

    with caplog.at_level(logging.DEBUG, logger="apm_cli.core.tls_trust"):
        log_tls_trust_status()

    assert _trust_source_messages(caplog) == ["TLS: verifying against OS trust store (truststore)"]


# ---------------------------------------------------------------------------
# T4 -- the internal bundled-default marker must NEVER leak out of
# configure_tls_trust, on ANY of its return branches. A leaked marker would tell
# a child interpreter to pop a SSL_CERT_FILE that is not actually a bundled
# default, silently weakening trust.
# ---------------------------------------------------------------------------


def _marker_env(**extra):
    env = {_BUNDLED_CERT_MARKER: "1"}
    env.update(extra)
    return env


def test_marker_cleared_on_opt_out_branch(monkeypatch):
    _install_fake_truststore(monkeypatch)
    env = _marker_env(**{_DISABLE_ENV_VAR: "1"})
    assert configure_tls_trust(env=env) is False
    assert _BUNDLED_CERT_MARKER not in env


def test_marker_cleared_on_explicit_override_branch(monkeypatch):
    _install_fake_truststore(monkeypatch)
    env = _marker_env(REQUESTS_CA_BUNDLE="/etc/ssl/corp.pem")
    assert configure_tls_trust(env=env) is False
    assert _BUNDLED_CERT_MARKER not in env


def test_marker_cleared_on_truststore_import_failure(monkeypatch):
    monkeypatch.setitem(sys.modules, "truststore", None)
    env = _marker_env(SSL_CERT_FILE="/bundled/certifi.pem")
    assert configure_tls_trust(env=env) is False
    assert _BUNDLED_CERT_MARKER not in env


def test_marker_cleared_on_inject_failure(monkeypatch):
    def _boom():
        raise RuntimeError("platform trust API unavailable")

    _install_fake_truststore(monkeypatch, inject=_boom)
    env = _marker_env(SSL_CERT_FILE="/bundled/certifi.pem")
    assert configure_tls_trust(env=env) is False
    assert _BUNDLED_CERT_MARKER not in env
    # certifi fallback restored (never zero trust).
    assert env.get("SSL_CERT_FILE") == "/bundled/certifi.pem"


def test_marker_cleared_on_inject_success(monkeypatch):
    _install_fake_truststore(monkeypatch)
    env = _marker_env(SSL_CERT_FILE="/bundled/certifi.pem")
    assert configure_tls_trust(env=env) is True
    assert _BUNDLED_CERT_MARKER not in env
    # bundled default popped so the OS store is consulted.
    assert "SSL_CERT_FILE" not in env


# ---------------------------------------------------------------------------
# build_child_tls_env retains its existing marker and PYTHONPATH hygiene.
# ---------------------------------------------------------------------------


def test_build_child_tls_env_strips_marker():
    base = {_BUNDLED_CERT_MARKER: "1", "PATH": "/usr/bin", "FOO": "bar"}
    child = build_child_tls_env(base)
    assert _BUNDLED_CERT_MARKER not in child
    assert child["PATH"] == "/usr/bin"
    assert child["FOO"] == "bar"


def test_build_child_tls_env_does_not_touch_pythonpath():
    base = {"PYTHONPATH": "/user/site"}
    child = build_child_tls_env(base)
    # No shim dir prepended -- a user/corporate PYTHONPATH survives untouched.
    assert child["PYTHONPATH"] == "/user/site"


def test_build_child_tls_env_returns_independent_copy():
    base = {"PATH": "/usr/bin"}
    child = build_child_tls_env(base)
    child["PATH"] = "/mutated"
    assert base["PATH"] == "/usr/bin"


# ---------------------------------------------------------------------------
# T7 -- ensure_child_tls_bootstrap drops both delivery artifacts into a venv's
# site-packages so the child interpreter can import the bootstrap.
# ---------------------------------------------------------------------------


def _fake_venv(tmp_path: Path) -> Path:
    """Create a POSIX-style venv skeleton with an empty site-packages dir."""
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    return tmp_path / "venv"


def test_ensure_child_tls_bootstrap_installs_both_files(tmp_path):
    venv = _fake_venv(tmp_path)
    assert ensure_child_tls_bootstrap(venv) is True

    site = venv / "lib" / "python3.12" / "site-packages"
    module = site / "_apm_tls_bootstrap.py"
    pth = site / "_apm_tls.pth"
    assert module.is_file()
    assert pth.is_file()
    # The .pth is exactly the one-line import that triggers the bootstrap.
    assert pth.read_text(encoding="utf-8").strip() == "import _apm_tls_bootstrap"
    # The bootstrap has no apm_cli dependency (self-contained).
    assert "import apm_cli" not in module.read_text(encoding="utf-8")


def test_ensure_child_tls_bootstrap_is_idempotent(tmp_path):
    venv = _fake_venv(tmp_path)
    assert ensure_child_tls_bootstrap(venv) is True
    assert ensure_child_tls_bootstrap(venv) is True


def test_ensure_child_tls_bootstrap_returns_false_for_missing_site_packages(tmp_path):
    # A path with no venv site-packages layout -> best-effort False, no raise.
    assert ensure_child_tls_bootstrap(tmp_path / "does-not-exist") is False


def test_ensure_child_tls_bootstrap_rejects_site_packages_symlink_escape(tmp_path):
    venv = tmp_path / "venv"
    python_dir = venv / "lib" / "python3.12"
    python_dir.mkdir(parents=True)
    outside = tmp_path / "shared-site-packages"
    outside.mkdir()
    try:
        (python_dir / "site-packages").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    assert ensure_child_tls_bootstrap(venv) is False
    assert not (outside / "_apm_tls_bootstrap.py").exists()
    assert not (outside / "_apm_tls.pth").exists()


# ---------------------------------------------------------------------------
# T2 (H1) -- the .pth is GENERATED inline, so delivery does not depend on the
# source .pth being packaged into the wheel (setuptools' packages.find drops
# stray .pth data files). Prove the content is produced, not copied.
# ---------------------------------------------------------------------------


def test_pth_is_generated_not_copied(tmp_path, monkeypatch):
    import apm_cli.core.tls_trust as tls

    venv = _fake_venv(tmp_path)

    # Point _child_bootstrap_dir at a source dir that has the MODULE but NO
    # .pth file -- if delivery copied the .pth it would fail; generation must
    # still produce it.
    fake_src = tmp_path / "shipped"
    fake_src.mkdir()
    (fake_src / "_apm_tls_bootstrap.py").write_text("# bootstrap\n", encoding="ascii")
    assert not (fake_src / "_apm_tls.pth").exists()
    monkeypatch.setattr(tls, "_child_bootstrap_dir", lambda: str(fake_src))

    assert ensure_child_tls_bootstrap(venv) is True

    site = venv / "lib" / "python3.12" / "site-packages"
    pth = site / "_apm_tls.pth"
    assert pth.is_file()
    # Exact generated content: a single import line the interpreter runs.
    assert pth.read_text(encoding="ascii") == "import _apm_tls_bootstrap\n"
    assert (site / "_apm_tls_bootstrap.py").read_text(encoding="ascii") == "# bootstrap\n"


def test_child_bootstrap_tls_policy_matches_parent_constants():
    import apm_cli.core.tls_trust as tls

    bootstrap = Path(tls._child_bootstrap_dir()) / "_apm_tls_bootstrap.py"
    literals = {
        node.value
        for node in ast.walk(ast.parse(bootstrap.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    expected = {
        tls._DISABLE_ENV_VAR,
        *tls._EXPLICIT_CA_ENV_VARS,
        tls._BUNDLED_CERT_MARKER,
        tls._SSL_CERT_FILE_VAR,
    }

    assert expected <= literals


def test_child_bootstrap_debug_messages_do_not_embed_console_symbols():
    import apm_cli.core.tls_trust as tls

    bootstrap = Path(tls._child_bootstrap_dir()) / "_apm_tls_bootstrap.py"
    assert '"[i] TLS:' not in bootstrap.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# T4 (M2) -- build_child_tls_env drops a BUNDLED certifi SSL_CERT_FILE so the
# child truststore reaches the OS store on Linux, but PRESERVES a genuine user
# SSL_CERT_FILE. The marker is still stripped either way.
# ---------------------------------------------------------------------------


def test_build_child_tls_env_drops_bundled_certifi_ssl_cert_file():
    import certifi

    base = {
        _BUNDLED_CERT_MARKER: "1",
        "SSL_CERT_FILE": certifi.where(),
        "PATH": "/usr/bin",
    }
    child = build_child_tls_env(base)
    assert "SSL_CERT_FILE" not in child
    assert _BUNDLED_CERT_MARKER not in child
    assert child["PATH"] == "/usr/bin"


def test_build_child_tls_env_drops_recorded_frozen_certifi_path(monkeypatch):
    import apm_cli.core.tls_trust as tls

    # The frozen hook path is recorded while its internal marker is present.
    # Exact matching avoids classifying an unrelated user path by suffix.
    frozen = "/tmp/_MEIabc123/certifi/cacert.pem"
    monkeypatch.setattr(tls, "_KNOWN_BUNDLED_CERT_FILE", frozen)
    child = build_child_tls_env({_BUNDLED_CERT_MARKER: "1", "SSL_CERT_FILE": frozen})
    assert "SSL_CERT_FILE" not in child


def test_build_child_tls_env_preserves_genuine_user_ssl_cert_file(tmp_path):
    user_ca = tmp_path / "corp" / "custom-ca.pem"
    user_ca.parent.mkdir(parents=True)
    user_ca.write_text("-----BEGIN CERTIFICATE-----\n", encoding="ascii")
    base = {"SSL_CERT_FILE": str(user_ca), "PATH": "/usr/bin"}
    child = build_child_tls_env(base)
    # A genuine user CA path must NEVER be dropped.
    assert child["SSL_CERT_FILE"] == str(user_ca)


def test_build_child_tls_env_preserves_certifi_lookalike_dir(monkeypatch):
    import apm_cli.core.tls_trust as tls

    # F1 (round-4): the match is on path COMPONENTS, not a raw suffix. A user
    # bundle under a directory that merely ENDS in "certifi" (e.g. a corporate
    # "mycertifi/") must be preserved, not mistaken for APM's bundled set.
    for lookalike in (
        "/opt/mycertifi/cacert.pem",
        "/opt/supercertifi/cacert.pem",
        "/a/notcertifi/cacert.pem",
    ):
        child = build_child_tls_env({_BUNDLED_CERT_MARKER: "1", "SSL_CERT_FILE": lookalike})
        assert child.get("SSL_CERT_FILE") == lookalike, f"lookalike {lookalike} was wrongly dropped"
    # Only the exact path recorded from the frozen hook matches.
    frozen = "/x/certifi/cacert.pem"
    monkeypatch.setattr(tls, "_KNOWN_BUNDLED_CERT_FILE", frozen)
    child = build_child_tls_env({_BUNDLED_CERT_MARKER: "1", "SSL_CERT_FILE": frozen})
    assert "SSL_CERT_FILE" not in child


def test_build_child_tls_env_preserves_user_certifi_component_path():
    user_bundle = "/opt/certifi/cacert.pem"
    child = build_child_tls_env({"SSL_CERT_FILE": user_bundle})

    assert child["SSL_CERT_FILE"] == user_bundle


# ---------------------------------------------------------------------------
# T5 (M3) -- a write failure must leave NO partial _apm_tls_bootstrap.py under a
# live .pth and must return False (atomic-write contract).
# ---------------------------------------------------------------------------


def test_ensure_child_tls_bootstrap_write_failure_leaves_no_partial(tmp_path, monkeypatch):
    import apm_cli.core.tls_trust as tls

    venv = _fake_venv(tmp_path)
    site = venv / "lib" / "python3.12" / "site-packages"

    def _boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(tls.os, "replace", _boom)

    assert ensure_child_tls_bootstrap(venv) is False
    # No partial artifacts left behind, and no leftover temp files.
    assert not (site / "_apm_tls_bootstrap.py").exists()
    assert not (site / "_apm_tls.pth").exists()
    assert list(site.glob(".apm_tls_*.tmp")) == []


# ---------------------------------------------------------------------------
# #2035 -- normal TLS failure guidance must match the selected trust mode.
# Before the fix the hint was unconditional: it recommended the OS trust store
# and APM_EXTRA_CA_BUNDLE even when an opt-out or an explicit replacement
# bundle made both ineffective.
# ---------------------------------------------------------------------------

# Advice from the former unconditional hint that the opt-out and replacement
# modes ignore. It must never be offered as a remedy in those modes.
_IGNORED_REMEDIES = (
    "installed in the OS trust store",
    f"set {_EXTRA_CA_ENV_VAR} to a readable PEM bundle",
)


def _guidance_after(env: dict[str, str]) -> str:
    """Select trust through the canonical owner, then render its guidance."""
    configure_tls_trust(env=env)
    return tls_failure_guidance()


def test_unavailable_selection_keeps_default_guidance_without_claiming_a_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import apm_cli.core.tls_trust as tls

    monkeypatch.setattr(tls, "_LAST_TLS_SELECTION", None)
    guidance = tls_failure_guidance()

    # The former unconditional text: it recommends both ignored remedies.
    assert all(remedy in guidance for remedy in _IGNORED_REMEDIES)
    assert "uses the system trust store by default" in guidance
    assert " is set" not in guidance


def test_opt_out_guidance_names_setting_and_drops_ignored_remedies() -> None:
    guidance = _guidance_after({_DISABLE_ENV_VAR: "1"})

    assert selected_tls_trust().source.value == "opt-out"
    assert f"{_DISABLE_ENV_VAR} is set" in guidance
    assert f"unset {_DISABLE_ENV_VAR}" in guidance
    assert "REQUESTS_CA_BUNDLE to a complete PEM bundle" in guidance
    assert not any(remedy in guidance for remedy in _IGNORED_REMEDIES)


@pytest.mark.parametrize(
    ("env", "winner"),
    [
        ({"REQUESTS_CA_BUNDLE": "/replacement/requests.pem"}, "REQUESTS_CA_BUNDLE"),
        ({"CURL_CA_BUNDLE": "/replacement/curl.pem"}, "CURL_CA_BUNDLE"),
        (
            {
                "REQUESTS_CA_BUNDLE": "/replacement/requests.pem",
                "CURL_CA_BUNDLE": "/replacement/curl.pem",
            },
            "REQUESTS_CA_BUNDLE",
        ),
    ],
    ids=["requests", "curl", "requests-over-curl"],
)
def test_replacement_guidance_names_winning_bundle(env: dict[str, str], winner: str) -> None:
    guidance = _guidance_after(env)

    assert f"{winner} is set and replaces the complete Requests trust set" in guidance
    assert f"unset {winner}" in guidance
    assert "the OS trust store and APM_EXTRA_CA_BUNDLE are not used" in guidance
    assert _DISABLE_ENV_VAR not in guidance
    assert not any(remedy in guidance for remedy in _IGNORED_REMEDIES)


def test_replacement_with_opt_out_names_both_settings() -> None:
    guidance = _guidance_after(
        {"CURL_CA_BUNDLE": "/replacement/curl.pem", _DISABLE_ENV_VAR: "1"},
    )

    selection = selected_tls_trust()
    assert selection.source.value == "replacement"
    assert selection.opt_out is True
    assert "CURL_CA_BUNDLE is set and replaces" in guidance
    assert f"{_DISABLE_ENV_VAR} is also set" in guidance
    assert "bundled CA set only" in guidance
    assert not any(remedy in guidance for remedy in _IGNORED_REMEDIES)


@pytest.mark.parametrize(
    "precedence",
    [{_DISABLE_ENV_VAR: "1"}, {"REQUESTS_CA_BUNDLE": "/replacement/requests.pem"}],
    ids=["opt-out", "replacement"],
)
def test_ignored_additive_bundle_is_not_recommended(
    tmp_path: Path, precedence: dict[str, str]
) -> None:
    env = {_EXTRA_CA_ENV_VAR: str(tmp_path / "must-not-be-read.pem"), **precedence}

    guidance = _guidance_after(env)

    assert f"OS trust store and {_EXTRA_CA_ENV_VAR} are not used" in guidance
    assert not any(remedy in guidance for remedy in _IGNORED_REMEDIES)


def test_os_store_guidance_is_unchanged_default(monkeypatch: pytest.MonkeyPatch) -> None:
    import apm_cli.core.tls_trust as tls

    _install_fake_truststore(monkeypatch)

    guidance = _guidance_after({})

    assert selected_tls_trust().source.value == "os-store"
    assert guidance == tls._DEFAULT_TLS_FAILURE_GUIDANCE


def test_os_store_additive_guidance_points_at_both_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import certifi

    import apm_cli.core.tls_trust as tls

    _install_fake_truststore(monkeypatch)
    monkeypatch.setattr(tls, "_install_additive_ca_context", lambda *_args: None)

    guidance = _guidance_after({_EXTRA_CA_ENV_VAR: certifi.where()})

    assert "OS trust store plus the APM_EXTRA_CA_BUNDLE certificates" in guidance
    assert "is set" not in guidance


def test_certifi_fallback_guidance_does_not_offer_os_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "truststore", None)

    guidance = _guidance_after({})

    assert selected_tls_trust().source.value == "certifi-fallback"
    assert "does not read the OS trust store" in guidance
    assert "installed in the OS trust store" not in guidance
    assert f"Set {_EXTRA_CA_ENV_VAR} to a readable PEM bundle" in guidance


def test_certifi_additive_guidance_points_at_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    import certifi

    import apm_cli.core.tls_trust as tls

    monkeypatch.setitem(sys.modules, "truststore", None)
    monkeypatch.setattr(tls, "_install_additive_ca_context", lambda *_args: None)

    guidance = _guidance_after({_EXTRA_CA_ENV_VAR: certifi.where()})

    assert selected_tls_trust().source.value == "certifi-additive"
    assert f"bundled CA set plus the {_EXTRA_CA_ENV_VAR} certificates" in guidance
    assert "installed in the OS trust store" not in guidance


@pytest.mark.parametrize(
    "env",
    [
        {},
        {_DISABLE_ENV_VAR: "1"},
        {"REQUESTS_CA_BUNDLE": "/replacement/requests.pem", _DISABLE_ENV_VAR: "1"},
    ],
    ids=["default", "opt-out", "replacement-opt-out"],
)
def test_failure_guidance_is_ascii_and_never_claims_cause(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    _install_fake_truststore(monkeypatch)

    guidance = _guidance_after(env)

    assert guidance.isascii()
    assert guidance.startswith("TLS verification failed -- ")
    assert "caused" not in guidance
    assert "ssl-issues" in guidance
