"""Warm-cache Codex installs carry the locked commit as the source pin (#3388).

Replays the issue's synthetic fixture (lockfile plus ``.apm-pin``) through an
in-process ``apm install --global --frozen``. The Codex client is stubbed just
past source-pin validation, so no native runtime is needed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.codex_plugins import registrar
from apm_cli.codex_plugins.client import CodexNativeError
from apm_cli.utils.content_hash import compute_package_hash
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [pytest.mark.component, pytest.mark.windows_compat]

_LOCKED_SHA = "f1b2ba49d18a3de11f87d8e6dc672dfdaadbff85"
_ISSUE_CONTENT_HASH = "sha256:4680f02c21d17cb85ce5feff3e1bfbe0966b7c2c42595e27a67c1d3eaecf6e30"
_SKILL_MD = (
    "---\n"
    "name: demo-skill\n"
    "description: A synthetic skill for reproducing install metadata handling.\n"
    "---\n"
    "\n"
    "# Demo\n"
    "\n"
    "This fixture has no executable resources or external dependencies.\n"
)
_PIN_ERROR = "Invalid original Codex source pin"
_CLIENT_REACHED = "stub: Codex client reached after source-pin validation"


def _write_issue_fixture(apm_home: Path, *, locked_commit: str, pin_commit: str | None) -> None:
    package = apm_home / "apm_modules" / "example" / "demo" / "skills" / "demo"
    (package / ".codex-plugin").mkdir(parents=True)
    (apm_home / "apm.yml").write_text(
        "name: synthetic-consumer\n"
        "version: 1.0.0\n"
        "targets: [codex]\n"
        "dependencies:\n"
        "  apm:\n"
        "    - example/demo/skills/demo\n",
        encoding="utf-8",
    )
    # newline="\n" keeps the bytes (and the pinned content hash) identical on Windows.
    (package / "SKILL.md").write_text(_SKILL_MD, encoding="utf-8", newline="\n")
    (package / ".codex-plugin" / "plugin.json").write_text(
        '{"name":"demo-skill"}\n', encoding="utf-8", newline="\n"
    )
    if pin_commit is not None:
        (package / ".apm-pin").write_text(
            json.dumps({"schema_version": 1, "resolved_commit": pin_commit}, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
    content_hash = compute_package_hash(package)
    assert content_hash == _ISSUE_CONTENT_HASH
    (apm_home / "apm.lock.yaml").write_text(
        "lockfile_version: '1'\n"
        "apm_version: 0.35.0\n"
        "dependencies:\n"
        "- repo_url: example/demo\n"
        "  name: demo-skill\n"
        "  version: 0.0.0\n"
        f"  resolved_commit: {locked_commit}\n"
        "  virtual_path: skills/demo\n"
        "  is_virtual: true\n"
        "  package_type: marketplace_plugin\n"
        f"  content_hash: {content_hash}\n"
        "deployments: []\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("locked_commit", "pin_commit", "expected_pin", "expected_error"),
    [
        pytest.param(_LOCKED_SHA, _LOCKED_SHA, _LOCKED_SHA, _CLIENT_REACHED, id="locked-sha"),
        pytest.param("f1b2ba4", "f1b2ba4", "cached", _PIN_ERROR, id="short-sha"),
        pytest.param("main", "main", "cached", _PIN_ERROR, id="branch-name"),
        pytest.param(_LOCKED_SHA, None, "cached", _PIN_ERROR, id="missing-pin"),
        pytest.param(_LOCKED_SHA, "a" * 40, "cached", _PIN_ERROR, id="inconsistent-pin"),
    ],
)
def test_frozen_global_install_from_warm_cache_uses_locked_source_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    locked_commit: str,
    pin_commit: str | None,
    expected_pin: str,
    expected_error: str,
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "cached-codex", base_env={})
    for key in list(os.environ):
        monkeypatch.delenv(key)
    for key, value in isolated.subprocess_env().items():
        monkeypatch.setenv(key, value)
    _write_issue_fixture(isolated.config_root, locked_commit=locked_commit, pin_commit=pin_commit)
    monkeypatch.chdir(isolated.work_root)

    source_pins: list[str | None] = []
    verify = registrar.verify_codex_source_identity

    def _record_pin(*args: object, **kwargs: object) -> object:
        source_pins.append(kwargs.get("source_commit"))  # type: ignore[arg-type]
        return verify(*args, **kwargs)  # type: ignore[arg-type]

    def _stub_client(*_args: object, **_kwargs: object) -> object:
        raise CodexNativeError(_CLIENT_REACHED)

    with (
        patch.object(registrar, "verify_codex_source_identity", _record_pin),
        patch("apm_cli.codex_plugins.client.CodexNativeClient.for_scope", _stub_client),
    ):
        result = CliRunner().invoke(cli, ["install", "--global", "--frozen", "--no-policy"])

    assert source_pins == [expected_pin], result.output
    output = " ".join(result.output.split())
    assert expected_error in output
    if expected_error == _CLIENT_REACHED:
        assert _PIN_ERROR not in output
    assert result.exit_code == 1
