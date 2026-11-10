"""Lifecycle script timeouts must be positive (#3435).

``timeoutSec`` and its ``timeout`` alias are one field. A non-positive value
under either spelling is invalid: the entry falls back to the per-type default
(30s for command, 10s for http) and exactly one warning names the script.
Positive values in either spelling are honored unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.core.lifecycle_scripts import parse_apm_yml_lifecycle, parse_script_file

_DEFAULTS = {"command": 30, "http": 10}


@pytest.fixture(autouse=True)
def _reset_warn_once():
    from apm_cli.core import lifecycle_scripts as ls

    ls._invalid_timeout_warned.clear()
    yield
    ls._invalid_timeout_warned.clear()


def _entry_yaml(script_type: str, key: str, value: object) -> str:
    target = (
        "      bash: echo hi\n" if script_type == "command" else "      url: https://x.test/h\n"
    )
    return (
        "lifecycle:\n"
        "  post-install:\n"
        "    - type: command\n"
        "      bash: echo first\n"
        f"    - type: {script_type}\n"
        f"{target}"
        f"      {key}: {value}\n"
    )


def _parse_yml(tmp_path: Path, content: str):
    manifest = tmp_path / "apm.yml"
    manifest.write_text(content, encoding="utf-8")
    with patch("apm_cli.utils.console._rich_warning") as warn:
        entries = parse_apm_yml_lifecycle(manifest, "project")
    return entries, [c.args[0] for c in warn.call_args_list], manifest


@pytest.mark.parametrize("script_type", ["command", "http"])
@pytest.mark.parametrize("key", ["timeoutSec", "timeout"])
@pytest.mark.parametrize("value", [0, -1, -5, -0.5])
def test_non_positive_timeout_falls_back_to_default_with_one_warning(
    tmp_path, script_type, key, value
):
    entries, warnings, manifest = _parse_yml(tmp_path, _entry_yaml(script_type, key, value))

    assert len(entries) == 2
    entry = entries[1]
    assert entry.timeout_sec is None
    assert entry.effective_timeout == _DEFAULTS[script_type]

    assert len(warnings) == 1
    msg = warnings[0]
    assert "post-install[1]" in msg
    assert script_type in msg
    assert str(manifest) in msg
    assert key in msg
    assert f"{_DEFAULTS[script_type]}s" in msg
    msg.encode("ascii")


@pytest.mark.parametrize("script_type", ["command", "http"])
@pytest.mark.parametrize("key", ["timeoutSec", "timeout"])
@pytest.mark.parametrize("value", [1, 5, 2.5])
def test_positive_timeout_is_honored_in_either_spelling(tmp_path, script_type, key, value):
    entries, warnings, _ = _parse_yml(tmp_path, _entry_yaml(script_type, key, value))

    assert entries[1].timeout_sec == value
    assert entries[1].effective_timeout == value
    assert warnings == []


def test_absent_timeout_uses_default_without_warning(tmp_path):
    content = "lifecycle:\n  post-install:\n    - type: http\n      url: https://x.test/h\n"
    entries, warnings, _ = _parse_yml(tmp_path, content)

    assert entries[0].timeout_sec is None
    assert entries[0].effective_timeout == 10
    assert warnings == []


def test_timeout_sec_wins_over_alias(tmp_path):
    content = (
        "lifecycle:\n"
        "  post-install:\n"
        "    - type: command\n"
        "      bash: echo hi\n"
        "      timeoutSec: 7\n"
        "      timeout: 3\n"
    )
    entries, warnings, _ = _parse_yml(tmp_path, content)

    assert entries[0].timeout_sec == 7
    assert warnings == []


def test_policy_json_non_positive_timeout_warns_once(tmp_path):
    policy_file = tmp_path / "10-hooks.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "scripts": {
                    "pre-install": [
                        {"type": "http", "url": "https://x.test/h", "timeoutSec": 0},
                        {"type": "command", "bash": "echo hi", "timeout": -3},
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    with patch("apm_cli.utils.console._rich_warning") as warn:
        entries = parse_script_file(policy_file, source="policy")

    assert [e.effective_timeout for e in entries] == [10, 30]
    assert all(e.timeout_sec is None for e in entries)
    messages = [c.args[0] for c in warn.call_args_list]
    assert len(messages) == 2
    assert "pre-install[0]" in messages[0]
    assert "pre-install[1]" in messages[1]


@pytest.fixture
def isolated_tiers(tmp_path, monkeypatch):
    """Project-only discovery: no policy dir, no user apm.yml, no org policy."""
    from apm_cli.core import lifecycle_scripts as ls

    monkeypatch.setattr(ls, "_get_policy_scripts_dir", lambda: tmp_path / "no-policy")
    monkeypatch.setattr(ls, "_get_user_apm_yml", lambda: tmp_path / "no-user" / "apm.yml")
    monkeypatch.setattr(
        "apm_cli.policy.discovery.discover_policy_with_chain", lambda *_a, **_k: None
    )
    project = tmp_path / "proj"
    project.mkdir()
    (project / "apm.yml").write_text(
        "name: t\n"
        "version: 0.0.1\n"
        "lifecycle:\n"
        "  pre-update:\n"
        "    - type: command\n"
        "      bash: echo hi\n"
        "      timeoutSec: 0\n",
        encoding="utf-8",
    )
    return project


class _Pkg:
    def __init__(self, path: Path) -> None:
        self.package_path = path

    def get_apm_dependencies(self) -> list:
        return []


def test_update_pre_and_post_events_warn_once(isolated_tiers):
    from apm_cli.commands.update import _fire_update_scripts

    with patch("apm_cli.utils.console._rich_warning") as warn:
        for event in ("pre-update", "post-update"):
            _fire_update_scripts(
                event, apm_package=_Pkg(isolated_tiers), scope="project", logger=None, verbose=False
            )

    messages = [c.args[0] for c in warn.call_args_list if "must be positive" in c.args[0]]
    assert len(messages) == 1


def test_uninstall_pre_and_post_events_warn_once(isolated_tiers):
    from apm_cli.commands.uninstall.cli import _fire_uninstall_scripts

    with patch("apm_cli.utils.console._rich_warning") as warn:
        for event in ("pre-uninstall", "post-uninstall"):
            _fire_uninstall_scripts(
                event,
                packages=[],
                scope="project",
                manifest_path=isolated_tiers / "apm.yml",
                logger=None,
                verbose=False,
                deploy_root=isolated_tiers,
            )

    messages = [c.args[0] for c in warn.call_args_list if "must be positive" in c.args[0]]
    assert len(messages) == 1
