"""Pin which project settings file GitHub Copilot obeys for a native plugin (#3921).

APM declares a project-scope plugin in ``.claude/settings.json`` for Claude
Code and in ``.github/copilot/settings.json`` for Copilot. Copilot reads both,
so the plugin can be named twice. This test is the contract for that overlap:
:data:`COPILOT_PROJECT_SETTINGS_PRECEDENCE` lists the files lowest priority
first, and the real Copilot CLI must agree, so a user's ``false`` in a later
file turns the plugin off for Copilot whatever an earlier file says.

It runs the ``copilot`` binary on PATH (or ``APM_COPILOT_BINARY``) with an
isolated ``COPILOT_HOME`` and only ``copilot plugin list``, which needs no
login; it skips when no binary is available.
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from apm_cli.claude_plugins.admission import ClaudeNativePlugin, fingerprint_native_claude_source
from apm_cli.claude_plugins.registrar import reconcile_native_plugins
from apm_cli.core.scope import InstallScope
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.native_plugins import scopes
from apm_cli.native_plugins.record import NativePluginRecord
from apm_cli.native_plugins.scopes import (
    CLAUDE,
    COPILOT,
    COPILOT_PROJECT_SETTINGS_PRECEDENCE,
    PROJECT_MARKETPLACE_PATH,
    PROJECT_SETTINGS_RELATIVE,
    project_marketplace_name,
)

pytestmark = pytest.mark.integration

_BINARY = os.environ.get("APM_COPILOT_BINARY") or shutil.which("copilot")


def test_precedence_names_both_files_apm_writes_lowest_first() -> None:
    order = COPILOT_PROJECT_SETTINGS_PRECEDENCE
    assert order.index(PROJECT_SETTINGS_RELATIVE[CLAUDE]) < order.index(
        PROJECT_SETTINGS_RELATIVE[COPILOT]
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    """A repository APM installed one Claude-format plugin into, at project scope."""
    return _install(tmp_path, monkeypatch, frozenset({CLAUDE, COPILOT}))


def _install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harnesses: frozenset[str]
) -> tuple[Path, str]:
    monkeypatch.setattr(scopes, "project_identity", lambda root: "example.test/acme/app")
    root = tmp_path / "repo"
    source = root / "apm_modules/acme/demo"
    (source / ".claude-plugin").mkdir(parents=True)
    (source / ".claude-plugin/plugin.json").write_text(
        json.dumps({"name": "demo", "version": "1.0.0", "description": "precedence pin"})
    )
    (source / "skills/hello").mkdir(parents=True)
    (source / "skills/hello/SKILL.md").write_text("---\nname: hello\ndescription: hi\n---\nhi\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    marketplace = project_marketplace_name(root, None, None)
    fingerprint = fingerprint_native_claude_source(source, portable=True)
    lock = LockFile()
    lock.dependencies["acme/demo"] = LockedDependency(
        repo_url="acme/demo", resolved_commit="a" * 40, package_type="marketplace_plugin"
    )
    lock.dependencies["acme/demo"].native_plugin = NativePluginRecord(
        "demo", fingerprint, marketplace, PROJECT_MARKETPLACE_PATH
    )
    reconcile_native_plugins(
        modules_dir=root / "apm_modules",
        scope=InstallScope.PROJECT,
        previous=None,
        lockfile=lock,
        prepared={
            "acme/demo": ClaudeNativePlugin(
                "acme/demo",
                "demo",
                source.resolve(),
                fingerprint,
                harnesses=harnesses,
            )
        },
        deploy_root=root,
        run_targets=frozenset({CLAUDE, COPILOT}),
    )
    return root, marketplace


def _copilot_state(root: Path, home: Path) -> str:
    home.mkdir(exist_ok=True)
    (home / "config.json").write_text(json.dumps({"trusted_folders": [str(root)]}))
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "COPILOT_HOME": str(home),
        "NO_COLOR": "1",
    }
    result = subprocess.run(
        [_BINARY, "plugin", "list"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    line = next((line for line in result.stdout.splitlines() if "demo@" in line), "")
    if "(enabled)" in line:
        return "enabled"
    if "(disabled)" in line:
        return "disabled"
    return f"absent: {result.stdout}{result.stderr}"


@pytest.mark.skipif(_BINARY is None, reason="no copilot binary on PATH")
def test_a_plugin_targeting_claude_only_stays_off_in_copilot(tmp_path, monkeypatch) -> None:
    """APM's own false in .github/copilot/settings.json beats .claude/settings.json."""
    root, marketplace = _install(tmp_path, monkeypatch, frozenset({CLAUDE}))
    claude = json.loads((root / PROJECT_SETTINGS_RELATIVE[CLAUDE]).read_text())
    assert claude["enabledPlugins"] == {f"demo@{marketplace}": True}
    assert _copilot_state(root, tmp_path / "copilot-home") == "disabled"


@pytest.mark.skipif(_BINARY is None, reason="no copilot binary on PATH")
def test_a_fresh_checkout_loads_the_apm_declared_plugin(repo, tmp_path) -> None:
    root, _ = repo
    assert _copilot_state(root, tmp_path / "copilot-home") == "enabled"


@pytest.mark.skipif(_BINARY is None, reason="no copilot binary on PATH")
@pytest.mark.parametrize(
    ("lower", "higher"), list(itertools.combinations(COPILOT_PROJECT_SETTINGS_PRECEDENCE, 2))
)
def test_the_later_file_wins_when_both_name_the_plugin(repo, tmp_path, lower, higher) -> None:
    root, marketplace = repo
    key = f"demo@{marketplace}"
    for relative in COPILOT_PROJECT_SETTINGS_PRECEDENCE:
        path = root / relative
        document = json.loads(path.read_text()) if path.exists() else {}
        document.setdefault("enabledPlugins", {}).pop(key, None)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document))
    for value, expected in ((False, "disabled"), (True, "enabled")):
        for relative, enabled in ((lower, not value), (higher, value)):
            path = root / relative
            document = json.loads(path.read_text())
            document["enabledPlugins"][key] = enabled
            path.write_text(json.dumps(document))
        assert _copilot_state(root, tmp_path / "copilot-home") == expected, (lower, higher)
