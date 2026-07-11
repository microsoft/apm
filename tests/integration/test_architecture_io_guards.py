"""Integration guardrails for process-wide output and credential I/O."""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path


def test_sbom_stdout_stays_machine_pure_when_update_is_available(
    tmp_path: Path, monkeypatch
) -> None:
    """Root output mode must route pre-command notifications off stdout."""
    from click.testing import CliRunner

    from apm_cli.cli import cli
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.utils.console import _reset_console, _rich_warning

    (tmp_path / "apm.yml").write_text("name: demo\nversion: 1.0.0\n", encoding="utf-8")
    LockFile().write(tmp_path / "apm.lock.yaml")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "apm_cli.cli._check_and_notify_updates",
        lambda: _rich_warning("A new version is available", symbol="warning"),
    )
    _reset_console()

    result = CliRunner().invoke(cli, ["lock", "export"])

    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["bomFormat"] == "CycloneDX"
    assert "A new version is available" not in result.stdout
    assert "A new version is available" in result.stderr


def test_descendant_logger_records_are_redacted(monkeypatch) -> None:
    """Handler-level redaction must cover every apm_cli descendant logger."""
    from apm_cli.cli import _configure_logging

    root = logging.getLogger()
    previous_handlers = list(root.handlers)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    monkeypatch.setattr(root, "handlers", [handler])

    _configure_logging(verbose=True)
    logging.getLogger("apm_cli.integration.mcp_integrator").warning("authorization: secret-value")

    rendered = stream.getvalue()
    assert "secret-value" not in rendered
    assert "[REDACTED]" in rendered
    root.handlers = previous_handlers


def test_unauthenticated_retry_receives_credential_free_environment(
    monkeypatch,
) -> None:
    """Authenticated state must not leak into a plain retry."""
    from apm_cli.core.auth import AuthContext, AuthResolver

    monkeypatch.setenv("GIT_TOKEN", "stale-token")
    monkeypatch.setenv("GIT_HTTP_EXTRAHEADER", "Authorization: secret")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "http.extraHeader")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "Authorization: secret")
    resolver = AuthResolver()
    host_info = resolver.classify_host("github.com")
    auth_env = resolver._build_git_env("active-token", host_kind=host_info.kind)
    monkeypatch.setattr(
        resolver,
        "resolve",
        lambda *_a, **_k: AuthContext(
            token="active-token",
            source="test",
            token_type="classic",
            host_info=host_info,
            git_env=auth_env,
        ),
    )
    attempts: list[tuple[str | None, dict]] = []

    def operation(token, env):
        attempts.append((token, env))
        if token is not None:
            raise RuntimeError("retry anonymously")
        return "ok"

    assert resolver.try_with_fallback("github.com", operation) == "ok"
    token, retry_env = attempts[-1]
    assert token is None
    assert "GIT_TOKEN" not in retry_env
    assert "GIT_HTTP_EXTRAHEADER" not in retry_env
    assert not any(key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")) for key in retry_env)
