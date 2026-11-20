"""Codex split install lifecycle (#3920): migrate, install, uninstall; never spawn codex.

Seeds the exact state the removed app-server route left behind (its ownership
journal, its snapshot and its two ``config.toml`` entries) next to entries the
user owns, then drives the real CLI in-process. A ``codex`` executable on PATH
records any invocation, so the test fails if APM ever runs the binary.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import pytest
import tomlkit
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.models.apm_package import DependencyReference
from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [pytest.mark.integration]


def _plugin_source(root: Path) -> None:
    (root / ".codex-plugin").mkdir(parents=True)
    (root / ".codex-plugin/plugin.json").write_text(
        json.dumps(
            {
                "name": "example",
                "version": "1.0.0",
                "skills": "./skills",
                "mcpServers": "./mcp.json",
            }
        ),
        encoding="utf-8",
    )
    (root / "mcp.json").write_text(
        json.dumps({"mcpServers": {"example": {"command": "DO_NOT_EXECUTE"}}}), encoding="utf-8"
    )
    skill = root / "skills/example/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: example\ndescription: Inert fixture\n---\nBody\n", encoding="utf-8"
    )


def _spy_codex(bin_dir: Path, marker: Path) -> None:
    """Put a codex executable first on PATH that only records that it ran."""
    bin_dir.mkdir()
    script = f"import pathlib; pathlib.Path({str(marker)!r}).write_text('ran')\n"
    (bin_dir / "codex-spy.py").write_text(script, encoding="utf-8")
    if os.name == "nt":
        (bin_dir / "codex.cmd").write_text(
            f'@"{sys.executable}" "{bin_dir / "codex-spy.py"}"\r\n', encoding="utf-8"
        )
    else:
        launcher = bin_dir / "codex"
        launcher.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{bin_dir / "codex-spy.py"}"\n', encoding="utf-8"
        )
        launcher.chmod(0o755)


def _seed_legacy_registration(apm_home: Path, codex_home: Path, key: str, *, enabled: bool) -> Path:
    modules = (apm_home / "apm_modules").resolve()
    marketplace = "apm-" + hashlib.sha256(f"{modules.as_posix()}\0{key}".encode()).hexdigest()[:24]
    snapshot = apm_home / ".codex-native/snapshots/0123"
    snapshot.mkdir(parents=True)
    (snapshot / "marker.txt").write_text("snapshot", encoding="utf-8")
    config = codex_home / "config.toml"
    (apm_home / ".codex-native/ownership.json").write_text(
        json.dumps(
            {
                "version": 1,
                "plugins": {
                    key: {
                        "name": "example",
                        "source_hash": "sha256:" + "c" * 64,
                        "snapshot": {
                            "path": str(snapshot),
                            "content_commit": "a" * 40,
                            "catalog_commit": "b" * 40,
                        },
                        "config_path": str(config),
                        "previous": None,
                        "pending": False,
                        "source_commit": None,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    config.write_text(
        'model = "gpt-5.4"\n\n'
        f'[plugins."example@{marketplace}"]\nenabled = {str(enabled).lower()}\n\n'
        '[plugins."foreign@external"]\nenabled = true\n\n'
        f'[marketplaces.{marketplace}]\nsource_type = "git"\n'
        f'source = "{snapshot.as_uri()}"\nref = "{"b" * 40}"\n\n'
        '[marketplaces.external]\nsource_type = "local"\nsource = "/elsewhere"\n',
        encoding="utf-8",
    )
    return snapshot


@pytest.fixture
def codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    inherited_path = os.environ.get("PATH", "")
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env={})
    for key in list(os.environ):
        monkeypatch.delenv(key)
    for key, value in isolated.subprocess_env().items():
        monkeypatch.setenv(key, value)
    codex = isolated.home / ".codex"
    codex.mkdir()
    marker = tmp_path / "codex-ran"
    _spy_codex(tmp_path / "bin", marker)
    monkeypatch.setenv("CODEX_HOME", str(codex))
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + inherited_path)
    monkeypatch.chdir(isolated.work_root)
    source = tmp_path / "source"
    _plugin_source(source)
    key = DependencyReference.parse(str(source)).get_unique_key()
    dump_yaml(
        {
            "name": "consumer",
            "version": "1.0.0",
            "targets": ["codex"],
            "dependencies": {"apm": [str(source)]},
            "allowExecutables": {str(source): {"mcp": True}},
        },
        isolated.config_root / "apm.yml",
    )
    yield isolated.config_root, codex, isolated.home, key, source, marker


def _run(*args: str) -> str:
    result = CliRunner().invoke(cli, list(args))
    assert result.exit_code == 0, result.output
    return " ".join(result.output.split())


def _config(codex: Path) -> dict:
    return tomlkit.parse((codex / "config.toml").read_text(encoding="utf-8")).unwrap()


def test_install_migrates_owned_registration_then_split_installs(codex_home) -> None:
    apm_home, codex, home, key, source, marker = codex_home
    snapshot = _seed_legacy_registration(apm_home, codex, key, enabled=False)

    output = _run("install", "--global", "--target", "codex", "--no-policy")

    assert (
        "Retired 1 Codex plugin registration(s) made by an earlier APM version: example" in output
    )
    assert "Codex: split-installed 1 plugin(s) at user scope (example)" in output
    assert "criterion A3" in output
    config = _config(codex)
    assert config["model"] == "gpt-5.4"
    assert config["plugins"] == {"foreign@external": {"enabled": True}}
    assert list(config["marketplaces"]) == ["external"]
    assert "example" in config["mcp_servers"]
    assert not snapshot.exists()
    assert not (apm_home / ".codex-native").exists()
    assert (home / ".agents/skills/example/SKILL.md").is_file()

    again = _run("install", "--global", "--target", "codex", "--no-policy")
    assert "Retired" not in again

    _run("uninstall", "--global", str(source))
    assert not (home / ".agents/skills/example").exists()
    config = _config(codex)
    assert config["plugins"] == {"foreign@external": {"enabled": True}}
    assert not marker.exists(), "APM must never run the codex binary"


def test_user_enabled_registration_is_left_in_place(codex_home) -> None:
    apm_home, codex, home, key, _source, marker = codex_home
    snapshot = _seed_legacy_registration(apm_home, codex, key, enabled=True)

    output = _run("install", "--global", "--target", "codex", "--no-policy")

    assert "Left 1 changed Codex plugin registration(s) in place: example" in output
    assert any(name.startswith("example@apm-") for name in _config(codex)["plugins"])
    assert snapshot.is_dir()
    assert not (apm_home / ".codex-native/ownership.json").exists()
    assert (home / ".agents/skills/example/SKILL.md").is_file()
    assert not marker.exists()


_REMOTE = "https://git.example.invalid/acme/example-plugin.git"


@pytest.mark.parametrize("downloads", ["parallel", "sequential"])
def test_cached_copy_from_the_retired_route_is_split_installed(
    codex_home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, downloads: str
) -> None:
    """An old-route ``apm_modules`` copy plus its lock row still deploys after retirement."""
    from apm_cli.utils.content_hash import compute_package_hash
    from tests.utils.local_git_repository import LocalGitRepositoryFactory

    apm_home, codex, home, _key, source, marker = codex_home
    repositories = LocalGitRepositoryFactory(tmp_path / "repositories", env=dict(os.environ))
    repository = repositories.create("example-plugin", source_tree=source)
    commit = repositories.commit(repository, message="publish example")
    repositories.tag(repository, "v1.0.0", commit)
    for name, value in repositories.url_rewrite_subprocess_env(repository, _REMOTE).items():
        monkeypatch.setenv(name, value)
    entry = {"git": _REMOTE, "ref": "v1.0.0"}
    dependency = DependencyReference.parse_from_dict(entry)
    key = dependency.get_unique_key()
    dump_yaml(
        {
            "name": "consumer",
            "version": "1.0.0",
            "targets": ["codex"],
            "dependencies": {"apm": [entry]},
            "allowExecutables": {key: {"mcp": True}},
        },
        apm_home / "apm.yml",
    )
    # The removed route kept the raw bundle in apm_modules and locked its hash.
    cached = dependency.get_install_path(apm_home / "apm_modules")
    cached.parent.mkdir(parents=True)
    shutil.copytree(source, cached)
    dump_yaml(
        {
            "lockfile_version": "1",
            "apm_version": "0.36.1",
            "dependencies": [
                {
                    "repo_url": dependency.repo_url,
                    "host": dependency.host,
                    "resolved_ref": "v1.0.0",
                    "resolved_commit": commit.sha,
                    "depth": 1,
                    "package_type": "marketplace_plugin",
                    "content_hash": compute_package_hash(cached),
                }
            ],
        },
        apm_home / "apm.lock.yaml",
    )
    snapshot = _seed_legacy_registration(apm_home, codex, key, enabled=False)

    install = ("install", "--global", "--target", "codex", "--no-policy")
    if downloads == "sequential":
        install = (*install, "--parallel-downloads", "0")
    output = _run(*install)

    assert "Retired 1 Codex plugin registration(s) made by an earlier APM version" in output
    assert "Codex: split-installed 1 plugin(s) at user scope (example)" in output
    assert not snapshot.exists()
    assert not any(name.startswith("example@apm-") for name in _config(codex)["plugins"])
    assert (home / ".agents/skills/example/SKILL.md").is_file(), output
    assert "example" in _config(codex).get("mcp_servers", {}), output
    assert not marker.exists(), "APM must never run the codex binary"

    lock_after = (apm_home / "apm.lock.yaml").read_bytes()
    again = _run(*install)
    assert "Retired" not in again
    assert (home / ".agents/skills/example/SKILL.md").is_file()
    assert (apm_home / "apm.lock.yaml").read_bytes() == lock_after
