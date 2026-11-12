"""End-to-end APM_HOME contract (#2884), exercised through real CLI processes.

``APM_HOME`` is read at process start, so each command runs the apm binary
under test in its own process with an explicit environment. No network access is needed:
the global package is a local path and marketplaces are pre-seeded.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]

_PACKAGE = "global-package"


def _write_package(root: Path) -> Path:
    package = root / _PACKAGE
    instructions = package / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    (package / "apm.yml").write_text(f"name: {_PACKAGE}\nversion: 0.1.0\n", encoding="utf-8")
    (instructions / "global.instructions.md").write_text(
        "---\ndescription: Global install instructions\n---\nUse integration tests.\n",
        encoding="utf-8",
    )
    skill = package / ".apm" / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo skill\n---\nBody.\n", encoding="utf-8"
    )
    return package


class _Env:
    def __init__(self, tmp_path: Path, apm_binary_path: Path) -> None:
        self.binary = str(apm_binary_path)
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.claude = tmp_path / "claude-config"
        self.cache = tmp_path / "cache"
        self.cwd = tmp_path / "work"
        self.cwd.mkdir()

    def run(self, apm_home: Path | str | None, *args: str) -> subprocess.CompletedProcess[str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"APM_HOME", "CLAUDE_CONFIG_DIR"}
        }
        env.update(
            {
                "HOME": str(self.home),
                "USERPROFILE": str(self.home),
                "CLAUDE_CONFIG_DIR": str(self.claude),
                "APM_E2E_TESTS": "1",
                "APM_CACHE_DIR": str(self.cache),
                "NO_COLOR": "1",
                "COLUMNS": "400",
            }
        )
        if apm_home is not None:
            env["APM_HOME"] = str(apm_home)
        return subprocess.run(
            (self.binary, *args),
            cwd=self.cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=180,
        )


def _ok(result: subprocess.CompletedProcess[str]) -> str:
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout + result.stderr


def _files_under(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def test_two_apm_homes_do_not_interfere(tmp_path: Path, apm_binary_path: Path) -> None:
    env = _Env(tmp_path, apm_binary_path)
    package = _write_package(tmp_path)
    home_a = tmp_path / "apm-a"
    home_b = tmp_path / "apm-b"

    # config
    _ok(env.run(home_a, "config", "set", "target", "claude"))
    assert "claude" in _ok(env.run(home_a, "config", "get", "target"))
    assert "claude" not in _ok(env.run(home_b, "config", "get", "target"))

    # install -g writes manifest, lockfile, modules and receipts under A only
    _ok(env.run(home_a, "install", "-g", str(package), "--target", "claude"))
    assert (home_a / "apm.yml").is_file()
    assert (home_a / "apm.lock.yaml").is_file()
    assert (home_a / "apm_modules").is_dir()
    assert any((home_a / "deployment-receipts").glob("*.json"))
    assert (env.claude / "rules" / "global.md").is_file()
    assert not (home_b / "apm.yml").exists()

    # deps and lock read the manifest of the selected home only
    assert _PACKAGE in _ok(env.run(home_a, "deps", "list", "-g"))
    assert _PACKAGE not in env.run(home_b, "deps", "list", "-g").stdout
    _ok(env.run(home_a, "lock", "-g"))
    missing = env.run(home_b, "lock", "-g")
    assert missing.returncode != 0
    assert str(home_b) in (missing.stdout + missing.stderr).replace("\n", "")

    # marketplace registrations follow APM_HOME
    (home_a / "marketplaces.json").write_text(
        json.dumps({"marketplaces": [{"name": "acme-tools", "owner": "acme", "repo": "tools"}]}),
        encoding="utf-8",
    )
    assert "acme-tools" in _ok(env.run(home_a, "marketplace", "list"))
    assert "acme-tools" not in env.run(home_b, "marketplace", "list").stdout

    # lifecycle trust lands in the selected home
    (env.cwd / "apm.yml").write_text(
        "name: work\nversion: 0.1.0\nlifecycle:\n  post-install:\n    - echo hi\n",
        encoding="utf-8",
    )
    _ok(env.run(home_b, "lifecycle", "trust"))
    assert (home_b / "scripts-trust.json").is_file()
    assert not (home_a / "scripts-trust.json").exists()

    # uninstall against B leaves A untouched; against A removes the package
    before = (home_a / "apm.yml").read_text(encoding="utf-8")
    env.run(home_b, "uninstall", "-g", str(package))
    assert (home_a / "apm.yml").read_text(encoding="utf-8") == before
    _ok(env.run(home_a, "uninstall", "-g", str(package)))
    assert _PACKAGE not in (home_a / "apm.yml").read_text(encoding="utf-8")

    # nothing APM-owned leaked into the default location; the OS home only
    # holds the deploy-root identity and the version-check cache (APM_CACHE_DIR
    # owns the download cache)
    assert not (env.home / ".apm").exists()
    assert {p.name for p in env.home.iterdir()} <= {".apm-local", ".cache"}


def test_unset_apm_home_keeps_the_default_layout(tmp_path: Path, apm_binary_path: Path) -> None:
    env = _Env(tmp_path, apm_binary_path)
    package = _write_package(tmp_path)
    default_home = env.home / ".apm"

    _ok(env.run(None, "config", "set", "target", "claude"))
    _ok(env.run(None, "install", "-g", str(package), "--target", "claude"))
    _ok(env.run(None, "lock", "-g"))

    files = _files_under(default_home)
    assert {"config.json", "apm.yml", "apm.lock.yaml"} <= files
    assert any(name.startswith("apm_modules/") for name in files)
    assert any(name.startswith("deployment-receipts/") for name in files)
    assert _PACKAGE in _ok(env.run(None, "deps", "list", "-g"))
    assert (env.claude / "rules" / "global.md").is_file()


def test_empty_apm_home_behaves_as_unset(tmp_path: Path, apm_binary_path: Path) -> None:
    env = _Env(tmp_path, apm_binary_path)
    _ok(env.run("", "config", "set", "target", "claude"))
    assert (env.home / ".apm" / "config.json").is_file()
