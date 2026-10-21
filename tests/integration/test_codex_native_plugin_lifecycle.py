"""Real installed APM and qualified Codex lifecycle; no agent or MCP process runs."""

from __future__ import annotations

import base64
import json
import os
import shutil
import stat
import sys
import uuid
from dataclasses import asdict
from pathlib import Path

import pytest
import tomlkit
import yaml

from apm_cli.codex_plugins.registrar import marketplace_name
from apm_cli.models.apm_package import DependencyReference
from apm_cli.utils.content_hash import compute_native_bundle_hash, compute_package_hash
from apm_cli.utils.path_security import safe_rmtree
from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_runtime_codex(version="0.163.0"),
]

_NATIVE_SCRIPT = """
import json, sys
from pathlib import Path
from apm_cli.codex_plugins.client import CodexNativeClient
client = CodexNativeClient(Path(sys.argv[1]), Path.cwd())
with client.connect() as rpc:
    result = rpc.call(sys.argv[2], json.loads(sys.argv[3]))
    print(json.dumps(result))
"""


def _native_result(
    codex: str, env: dict[str, str], root: Path, method: str, params: dict
) -> CommandResult:
    """Drive real public API operations with the same credential-free fixture environment."""
    result = ApmLifecycleRunner((sys.executable, "-c", _NATIVE_SCRIPT, codex)).run(
        (method, json.dumps(params)),
        cwd=root,
        env=env,
        scenario_id=method,
    )
    (root / f"native-{uuid.uuid4().hex}-evidence.json").write_text(
        json.dumps(asdict(result), default=str, indent=2), encoding="utf-8"
    )
    return result


def _native(codex: str, env: dict[str, str], root: Path, method: str, params: dict) -> dict:
    result = _native_result(codex, env, root, method, params)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _native_write(codex: str, env: dict[str, str], root: Path, values: dict) -> None:
    config = _native(codex, env, root, "config/read", {"includeLayers": True})
    layer = next(layer for layer in config["layers"] if layer["name"]["type"] == "user")
    result = _native(
        codex,
        env,
        root,
        "config/batchWrite",
        {
            "expectedVersion": layer["version"],
            "edits": [
                {"keyPath": key, "value": value, "mergeStrategy": "replace"}
                for key, value in values.items()
            ],
        },
    )
    assert result["status"] == "ok"


def _apm(
    runner: ApmLifecycleRunner,
    args: tuple[str, ...],
    root: Path,
    env: dict[str, str],
    label: str,
) -> CommandResult:
    result = runner.run(args, cwd=root, env=env, scenario_id=label)
    (root / f"{label}-evidence.json").write_text(
        json.dumps(asdict(result), default=str, indent=2),
        encoding="utf-8",
    )
    return result


def _environment(root: Path) -> dict[str, str]:
    """Do not inherit credentials, native trust, or unrelated user configuration."""
    home = root / "home"
    home.mkdir()
    (home / ".codex").mkdir()
    return {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "CODEX_HOME": str(home / ".codex"),
        "APM_HOME": str(home / ".apm"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "PATH": os.environ["PATH"],
        "LANG": "en_US.UTF-8",
        "NO_COLOR": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "file",
        **{key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ},
    }


def _source(root: Path, marker: str) -> None:
    """Write only inert source data, including a sentinel that must never execute."""
    (root / ".codex-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".codex-plugin/plugin.json").write_text(
        json.dumps(
            {
                "name": "example",
                "version": "1.0.0",
                "skills": "./skills",
                "mcpServers": "./.mcp.codex.json",
            }
        ),
        encoding="utf-8",
    )
    (root / ".mcp.codex.json").write_text(
        json.dumps({"mcpServers": {"example": {"command": "DO_NOT_EXECUTE", "args": [marker]}}}),
        encoding="utf-8",
    )
    skill = root / "skills/example/SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(
        f"---\nname: example\ndescription: Inert native fixture\n---\n{marker}\n",
        encoding="utf-8",
    )
    (root / "sibling.txt").write_text(marker, encoding="utf-8")
    asset = root / "resources/__pycache__/asset.bin"
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(marker.encode())
    asset.chmod(0o644)


def _audit_cache(
    runner: ApmLifecycleRunner,
    root: Path,
    env: dict[str, str],
    materialized: Path,
) -> None:
    """Native source cache drift must fail the canonical audit without native writes."""
    home = Path(env["HOME"])

    def state() -> dict[Path, tuple[bytes, int]]:
        return {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in home.rglob("*")
            if path.is_file()
        }

    def audit(label: str, *, json_output: bool = False) -> CommandResult:
        before = state()
        result = runner.run(
            ("audit", "--ci", "--no-policy", *(("--format", "json") if json_output else ())),
            cwd=Path(env["APM_HOME"]),
            env=env,
            scenario_id=label,
        )
        (root / f"{label}-evidence.json").write_text(
            json.dumps(asdict(result), default=str, indent=2), encoding="utf-8"
        )
        assert state() == before
        return result

    clean = audit("codex-native-cache-audit-clean")
    assert clean.returncode == 0, clean.stdout + clean.stderr
    for relative in ("sibling.txt", "resources/__pycache__/asset.bin"):
        cached = materialized / relative
        original = cached.read_bytes()
        try:
            cached.write_bytes(original + b"\nChanged only in the APM cache.\n")
            changed = audit(f"codex-native-cache-audit-tampered-{cached.name}")
            assert changed.returncode != 0, changed.stdout + changed.stderr
            assert "cache differs" in " ".join((changed.stdout + changed.stderr).split())
            assert "Traceback" not in changed.stderr
            structured = audit(f"codex-native-cache-json-{cached.name}", json_output=True)
            assert structured.returncode != 0, structured.stdout + structured.stderr
            report = json.loads(structured.stdout)
            assert report["passed"] is False
            assert any(
                check["name"] == "drift"
                and check["passed"] is False
                and "cache differs" in check["message"]
                for check in report["checks"]
            )
        finally:
            cached.write_bytes(original)
    if os.name != "nt":
        cached = materialized / "resources/__pycache__/asset.bin"
        original_mode = cached.stat().st_mode
        try:
            cached.chmod(original_mode ^ stat.S_IXUSR)
            changed = audit("codex-native-cache-audit-mode")
            assert changed.returncode != 0, changed.stdout + changed.stderr
            assert "cache differs" in " ".join((changed.stdout + changed.stderr).split())
            assert "Traceback" not in changed.stderr
        finally:
            cached.chmod(original_mode)


@pytest.mark.parametrize("mcp_shape", ["file", "inline"])
def test_installed_cli_registers_complete_codex_bundle_disabled(
    tmp_path: Path,
    apm_binary_path: Path,
    mcp_shape: str,
) -> None:
    """The real install cannot claim native delivery after a skills-only projection."""
    codex = shutil.which("codex")
    assert codex is not None
    env = _environment(tmp_path)
    source = tmp_path / "source"
    _source(source, "FIRST")
    if mcp_shape == "inline":
        manifest = source / ".codex-plugin/plugin.json"
        document = json.loads(manifest.read_text())
        document["mcpServers"] = json.loads((source / ".mcp.codex.json").read_text())["mcpServers"]
        manifest.write_text(json.dumps(document), encoding="utf-8")
    apm_home = Path(env["APM_HOME"])
    apm_home.mkdir()
    reference = DependencyReference.parse(str(source))
    dump_yaml(
        {
            "name": "consumer",
            "version": "1.0.0",
            "targets": ["codex"],
            "dependencies": {"apm": [str(source)]},
            "allowExecutables": {str(reference): {"mcp": True}},
        },
        apm_home / "apm.yml",
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    result = _apm(
        runner,
        ("install", "-g", "--target", "codex", "--no-policy"),
        tmp_path,
        env,
        "codex-native-first",
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    config_path = Path(env["CODEX_HOME"]) / "config.toml"
    assert config_path.is_file(), result.stdout
    config = tomlkit.parse(config_path.read_text()).unwrap()
    owned = [key for key in config.get("plugins", {}) if key.startswith("example@apm-")]
    assert len(owned) == 1
    assert config["plugins"][owned[0]] == {"enabled": False}
    assert not list((Path(env["HOME"]) / ".agents/skills").glob("*"))
    assert all(path.name == ".system" for path in (Path(env["HOME"]) / ".codex/skills").glob("*"))
    ledger = apm_home / ".codex-native/ownership.json"
    original_ledger = ledger.read_bytes()
    record = next(iter(json.loads(original_ledger)["plugins"].values()))
    installed = Path(record["installed_root"])
    assert compute_package_hash(installed) == compute_package_hash(source)
    _audit_cache(runner, tmp_path, env, reference.get_install_path(apm_home / "apm_modules"))
    detail = _native(
        codex,
        env,
        tmp_path,
        "plugin/read",
        {
            "marketplacePath": record["marketplace_path"],
            "pluginName": "example",
        },
    )["plugin"]
    assert detail["mcpServers"] == ["example"]
    assert detail["summary"]["enabled"] is False
    _native_write(
        codex,
        env,
        tmp_path,
        {
            f'plugins."{owned[0]}".mcp_servers.example.enabled': False,
            'plugins."foreign@external"': {"enabled": False},
            "model": "gpt-5.3-codex",
        },
    )
    managed_config = config_path.read_bytes()
    stale = _native(codex, env, tmp_path, "config/read", {"includeLayers": True})
    stale_layer = next(layer for layer in stale["layers"] if layer["name"]["type"] == "user")
    _native_write(codex, env, tmp_path, {"model": "gpt-5.4"})
    concurrent_config = config_path.read_bytes()
    rejected = _native_result(
        codex,
        env,
        tmp_path,
        "config/batchWrite",
        {
            "expectedVersion": stale_layer["version"],
            "edits": [{"keyPath": "model", "value": "must-not-write", "mergeStrategy": "replace"}],
        },
    )
    assert rejected.returncode != 0
    assert config_path.read_bytes() == concurrent_config
    _native_write(codex, env, tmp_path, {"model": "gpt-5.3-codex"})
    managed_config = config_path.read_bytes()
    result = _apm(
        runner,
        ("install", "-g", "--frozen", "--target", "codex", "--no-policy"),
        tmp_path,
        env,
        "codex-native-frozen-noop",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert ledger.read_bytes() == original_ledger
    assert config_path.read_bytes() == managed_config

    (source / "resources/__pycache__/asset.bin").chmod(0o755)
    result = _apm(
        runner,
        ("install", "-g", "--target", "codex", "--no-policy"),
        tmp_path,
        env,
        "codex-native-executable-mode-update",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    record = next(iter(json.loads(ledger.read_bytes())["plugins"].values()))
    installed = Path(record["installed_root"])
    assert compute_native_bundle_hash(installed) == compute_native_bundle_hash(source)
    assert (
        record["source_hash"]
        != next(iter(json.loads(original_ledger)["plugins"].values()))["source_hash"]
    )
    assert tomlkit.parse(config_path.read_text()).unwrap()["plugins"][owned[0]] == {
        "enabled": False,
        "mcp_servers": {"example": {"enabled": False}},
    }

    for label, method, params in (
        (
            "cache-named-resource",
            "fs/writeFile",
            {
                "path": str(installed / "resources/__pycache__/asset.bin"),
                "dataBase64": base64.b64encode(b"CORRUPTED").decode(),
            },
        ),
        (
            "corrupt",
            "fs/writeFile",
            {
                "path": str(installed / "sibling.txt"),
                "dataBase64": base64.b64encode(b"CORRUPTED").decode(),
            },
        ),
        ("missing", "fs/remove", {"path": str(installed), "recursive": True, "force": False}),
    ):
        _native(codex, env, tmp_path, method, params)
        assert compute_native_bundle_hash(installed) != compute_native_bundle_hash(source)
        before = next(iter(json.loads(ledger.read_bytes())["plugins"].values()))
        result = _apm(
            runner,
            ("install", "-g", "--frozen", "--target", "codex", "--no-policy"),
            tmp_path,
            env,
            f"codex-native-{label}-restore",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        after = next(iter(json.loads(ledger.read_bytes())["plugins"].values()))
        assert after["snapshot"]["content_commit"] == before["snapshot"]["content_commit"]
        assert after["snapshot"]["catalog_commit"] != before["snapshot"]["catalog_commit"]
        assert compute_native_bundle_hash(
            Path(after["installed_root"])
        ) == compute_native_bundle_hash(source)
        state = tomlkit.parse(config_path.read_text()).unwrap()
        assert state["plugins"][owned[0]] == {
            "enabled": False,
            "mcp_servers": {"example": {"enabled": False}},
        }
        assert state["plugins"]["foreign@external"] == {"enabled": False}
        assert state["model"] == "gpt-5.3-codex"

    publisher_manifest = (source / ".codex-plugin/plugin.json").read_bytes()
    before = next(iter(json.loads(ledger.read_bytes())["plugins"].values()))
    lock_before = (apm_home / "apm.lock.yaml").read_bytes()
    safe_rmtree(Path(before["snapshot"]["path"]), ledger.parent / "snapshots")
    result = _apm(
        runner,
        ("install", "-g", "--frozen", "--target", "codex", "--no-policy"),
        tmp_path,
        env,
        "codex-native-missing-transport-restore",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    record = next(iter(json.loads(ledger.read_bytes())["plugins"].values()))
    assert record["snapshot"]["path"] != before["snapshot"]["path"]
    assert (apm_home / "apm.lock.yaml").read_bytes() == lock_before
    assert compute_package_hash(Path(record["installed_root"])) == compute_package_hash(source)
    _native_write(codex, env, tmp_path, {f'plugins."{owned[0]}".enabled': True})
    _source(source, "SAME_VERSION_CHANGED_BYTES")
    if mcp_shape == "inline":
        document = json.loads(publisher_manifest)
        document["mcpServers"]["example"]["args"] = ["SAME_VERSION_CHANGED_BYTES"]
        (source / ".codex-plugin/plugin.json").write_text(json.dumps(document), encoding="utf-8")
        publisher_manifest = (source / ".codex-plugin/plugin.json").read_bytes()
    result = _apm(
        runner,
        ("install", "-g", "--update", "--target", "codex", "--no-policy"),
        tmp_path,
        env,
        "codex-native-same-version-update",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    record = next(iter(json.loads(ledger.read_bytes())["plugins"].values()))
    assert compute_package_hash(Path(record["installed_root"])) == compute_package_hash(source)
    assert (source / ".codex-plugin/plugin.json").read_bytes() == publisher_manifest
    assert tomlkit.parse(config_path.read_text()).unwrap()["plugins"][owned[0]]["enabled"] is True
    mcp = ApmLifecycleRunner((codex,)).run(("mcp", "list", "--json"), cwd=tmp_path, env=env)
    assert mcp.returncode == 0, mcp.stderr
    discovered = json.loads(mcp.stdout)
    assert any(
        server["transport"].get("args") == ["SAME_VERSION_CHANGED_BYTES"] for server in discovered
    )
    assert all(server["enabled"] is False for server in discovered)
    manifest_before = (apm_home / "apm.yml").read_bytes()
    native_before = config_path.read_bytes()
    result = _apm(
        runner,
        ("uninstall", "-g", str(source)),
        tmp_path,
        env,
        "codex-native-customized-retained",
    )
    assert result.returncode != 0, result.stdout
    assert "user-managed settings" in result.stdout + result.stderr
    assert (apm_home / "apm.yml").read_bytes() == manifest_before
    assert config_path.read_bytes() == native_before
    assert Path(record["installed_root"]).is_dir()

    # The fixture operator explicitly retires its custom policy before asking APM to remove.
    _native_write(codex, env, tmp_path, {f'plugins."{owned[0]}"': {"enabled": False}})
    result = _apm(
        runner,
        ("uninstall", "-g", str(source)),
        tmp_path,
        env,
        "codex-native-owned-removal",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    state = tomlkit.parse(config_path.read_text()).unwrap()
    assert owned[0] not in state.get("plugins", {})
    assert state["plugins"]["foreign@external"] == {"enabled": False}
    assert state["model"] == "gpt-5.3-codex"
    assert not Path(record["installed_root"]).exists()
    assert json.loads(ledger.read_bytes())["plugins"] == {}


@pytest.mark.parametrize("version", [None, "0.0.0"])
def test_git_pinned_install_and_missing_apm_cache_restore(
    tmp_path: Path, apm_binary_path: Path, version: str | None
) -> None:
    """Git materialization, original source pin, and native transport identity stay separate."""
    codex = shutil.which("codex")
    assert codex is not None
    env = _environment(tmp_path)
    env.update(
        {
            "GIT_AUTHOR_NAME": "Owned fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Owned fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        }
    )
    source = tmp_path / "publisher"
    _source(source, "PINNED_GIT")
    manifest = source / ".codex-plugin/plugin.json"
    document = json.loads(manifest.read_text())
    if version is None:
        del document["version"]
    else:
        document["version"] = version
    manifest.write_text(json.dumps(document), encoding="utf-8")
    publisher_metadata = "name: publisher-metadata\nversion: 0.0.0\n"
    (source / "apm.yml").write_text(publisher_metadata, encoding="utf-8")
    factory = LocalGitRepositoryFactory(tmp_path / "repositories", env=env)
    repository = factory.create("codex-fixture", source_tree=source)
    commit = factory.commit(repository, message="Inert native compatibility fixture")
    remote = "https://git.example.invalid/owner/native.git"
    env = factory.url_rewrite_subprocess_env(repository, remote)
    declaration = f"{remote}#{commit.sha}"
    reference = DependencyReference.parse(declaration)
    apm_home = Path(env["APM_HOME"])
    apm_home.mkdir()
    dump_yaml(
        {
            "name": "git-consumer",
            "version": "1.0.0",
            "targets": ["codex"],
            "dependencies": {
                "apm": [declaration],
                "mcp": [
                    {
                        "name": "loose",
                        "registry": False,
                        "command": "DO_NOT_EXECUTE",
                        "transport": "stdio",
                    }
                ],
            },
            "allowExecutables": {str(reference): {"mcp": True}, "loose": {"mcp": True}},
        },
        apm_home / "apm.yml",
    )
    _native_write(
        codex, env, tmp_path, {"mcp_servers.loose": {"command": "DO_NOT_EXECUTE", "enabled": False}}
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    result = _apm(
        runner,
        ("install", "-g", "--target", "codex", "--no-policy"),
        tmp_path,
        env,
        "codex-native-git-install",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    lock_path = apm_home / "apm.lock.yaml"
    lock_before = lock_path.read_bytes()
    assert commit.sha in lock_before.decode()
    ledger = apm_home / ".codex-native/ownership.json"
    ownership_before = ledger.read_bytes()
    record = next(iter(json.loads(ownership_before)["plugins"].values()))
    assert record["source_commit"] == commit.sha
    assert compute_package_hash(Path(record["installed_root"])) == compute_package_hash(source)
    materialized = reference.get_install_path(apm_home / "apm_modules")
    assert (materialized / "apm.yml").read_text() == publisher_metadata
    assert (
        tomlkit.parse((Path(env["CODEX_HOME"]) / "config.toml").read_text()).unwrap()[
            "mcp_servers"
        ]["loose"]["enabled"]
        is False
    )
    _audit_cache(runner, tmp_path, env, materialized)
    config_path = Path(env["CODEX_HOME"]) / "config.toml"
    config_before = config_path.read_bytes()
    asset = materialized / "resources/__pycache__/asset.bin"
    asset_before = asset.read_bytes()
    mode_before = asset.stat().st_mode
    for fault in ("bytes", "mode"):
        if fault == "mode" and os.name == "nt":
            continue
        if fault == "bytes":
            asset.write_bytes(b"changed at the same original Git pin")
        else:
            asset.chmod(mode_before ^ stat.S_IXUSR)
        try:
            refused = _apm(
                runner,
                ("install", "-g", "--frozen", "--target", "codex", "--no-policy"),
                tmp_path,
                env,
                f"codex-native-same-pin-{fault}",
            )
            assert refused.returncode != 0, refused.stdout + refused.stderr
            assert "cache differs" in " ".join((refused.stdout + refused.stderr).split())
            assert "Error installing dependencies:" not in refused.stdout + refused.stderr
            assert ledger.read_bytes() == ownership_before
            assert config_path.read_bytes() == config_before
            assert lock_path.read_bytes() == lock_before
        finally:
            asset.write_bytes(asset_before)
            asset.chmod(mode_before)
    legacy = json.loads(ownership_before)
    for entry in legacy["plugins"].values():
        del entry["source_commit"]
    ledger.write_text(json.dumps(legacy), encoding="utf-8")
    legacy_before = ledger.read_bytes()
    try:
        refused = _apm(
            runner,
            ("install", "-g", "--frozen", "--target", "codex", "--no-policy"),
            tmp_path,
            env,
            "codex-native-missing-source-provenance",
        )
        assert refused.returncode != 0, refused.stdout + refused.stderr
        assert "source provenance is missing" in " ".join((refused.stdout + refused.stderr).split())
        assert "<saved-pinned-dependency>" in refused.stdout + refused.stderr
        assert ledger.read_bytes() == legacy_before
        assert config_path.read_bytes() == config_before
    finally:
        ledger.write_bytes(ownership_before)
    safe_rmtree(materialized, apm_home / "apm_modules")
    result = _apm(
        runner,
        ("install", "-g", "--frozen", "--target", "codex", "--no-policy"),
        tmp_path,
        env,
        "codex-native-git-restore",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert lock_path.read_bytes() == lock_before
    assert ledger.read_bytes() == ownership_before
    assert compute_package_hash(materialized) == compute_package_hash(source)
    (repository.worktree / "resources/__pycache__/asset.bin").write_bytes(b"new pinned release")
    updated_commit = factory.commit(repository, message="Update opaque publisher resource")
    updated_declaration = f"{remote}#{updated_commit.sha}"
    document = yaml.safe_load((apm_home / "apm.yml").read_text())
    document["dependencies"]["apm"] = [updated_declaration]
    document["allowExecutables"] = {
        str(DependencyReference.parse(updated_declaration)): {"mcp": True},
        "loose": {"mcp": True},
    }
    dump_yaml(document, apm_home / "apm.yml")
    updated = _apm(
        runner,
        ("install", "-g", "--target", "codex", "--no-policy"),
        tmp_path,
        env,
        "codex-native-new-source-pin",
    )
    assert updated.returncode == 0, updated.stdout + updated.stderr
    updated_record = next(iter(json.loads(ledger.read_bytes())["plugins"].values()))
    assert updated_record["source_commit"] == updated_commit.sha
    assert compute_native_bundle_hash(
        Path(updated_record["installed_root"])
    ) == compute_native_bundle_hash(repository.worktree)
    git = ApmLifecycleRunner(("git", "-c", f"core.hooksPath={os.devnull}"))
    for repair in (False, True):
        for command in (
            ("commit", "--allow-empty", "-m", f"Same native payload, repair={repair}"),
            ("push", "origin", "HEAD:refs/heads/main"),
        ):
            result = git.run(
                command, cwd=repository.worktree, env=env, scenario_id="codex-empty-pin"
            )
            assert result.returncode == 0, result.stdout + result.stderr
        revision = git.run(
            ("rev-parse", "HEAD"),
            cwd=repository.worktree,
            env=env,
            scenario_id="codex-empty-pin-id",
        )
        assert revision.returncode == 0, revision.stdout + revision.stderr
        selected_pin = revision.stdout.strip()
        selected_declaration = f"{remote}#{selected_pin}"
        document["dependencies"]["apm"] = [selected_declaration]
        document["allowExecutables"] = {
            str(DependencyReference.parse(selected_declaration)): {"mcp": True},
            "loose": {"mcp": True},
        }
        dump_yaml(document, apm_home / "apm.yml")
        config_before = config_path.read_bytes()
        previous_record = updated_record
        if repair:
            (
                Path(previous_record["installed_root"]) / "resources/__pycache__/asset.bin"
            ).write_bytes(b"corrupt owned native copy, not publisher source")
        result = _apm(
            runner,
            ("install", "-g", "--target", "codex", "--no-policy"),
            tmp_path,
            env,
            f"codex-native-identical-source-pin-repair-{repair}",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        updated_record = next(iter(json.loads(ledger.read_bytes())["plugins"].values()))
        assert updated_record["source_commit"] == selected_pin
        assert updated_record["source_hash"] == previous_record["source_hash"]
        assert compute_native_bundle_hash(
            Path(updated_record["installed_root"])
        ) == compute_native_bundle_hash(repository.worktree)
        if not repair:
            assert updated_record["snapshot"] == previous_record["snapshot"]
            assert config_path.read_bytes() == config_before
    ownership_before = ledger.read_bytes()
    config_before = config_path.read_bytes()
    asset_before = asset.read_bytes()
    try:
        asset.write_bytes(b"changed after same-content pin repair")
        refused = _apm(
            runner,
            ("install", "-g", "--frozen", "--target", "codex", "--no-policy"),
            tmp_path,
            env,
            "codex-native-repaired-pin-cache-refusal",
        )
        assert refused.returncode != 0, refused.stdout + refused.stderr
        assert "cache differs" in " ".join((refused.stdout + refused.stderr).split())
        assert ledger.read_bytes() == ownership_before
        assert config_path.read_bytes() == config_before
    finally:
        asset.write_bytes(asset_before)


def test_migration_preserves_edited_loose_skill_without_native_registration(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A real old loose install cannot acquire duplicate native ownership."""
    env = _environment(tmp_path)
    source = tmp_path / "source"
    _source(source, "OLD_LOOSE")
    manifest = source / ".codex-plugin/plugin.json"
    native_manifest = manifest.read_bytes()
    manifest.unlink()
    (source / "SKILL.md").write_bytes((source / "skills/example/SKILL.md").read_bytes())
    apm_home = Path(env["APM_HOME"])
    apm_home.mkdir()
    reference = DependencyReference.parse(str(source))
    dump_yaml(
        {
            "name": "migration-consumer",
            "version": "1.0.0",
            "targets": ["codex"],
            "dependencies": {"apm": [str(source)]},
            "allowExecutables": {str(reference): {"mcp": True}},
        },
        apm_home / "apm.yml",
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    args = ("install", "-g", "--target", "codex", "--no-policy")
    old = _apm(runner, args, tmp_path, env, "codex-old-loose-install")
    assert old.returncode == 0, old.stdout + old.stderr
    home = Path(env["HOME"])
    loose = [
        path
        for root in (home / ".agents/skills", home / ".codex/skills")
        for path in root.glob("*/SKILL.md")
        if ".system" not in path.parts
    ]
    assert len(loose) == 1
    loose[0].write_text("User-edited loose skill\n", encoding="utf-8")
    edited = loose[0].read_bytes()
    manifest.write_bytes(native_manifest)
    (source / "SKILL.md").unlink()
    migrated = _apm(runner, args, tmp_path, env, "codex-migration-user-edit-refusal")
    assert migrated.returncode != 0, migrated.stdout + migrated.stderr
    diagnostic = " ".join((migrated.stdout + migrated.stderr).split())
    assert "retained loose" in diagnostic
    assert ".agents/skills/source/SKILL.md" in diagnostic
    assert "apm install -g --target codex" in diagnostic
    assert loose[0].read_bytes() == edited
    assert not (apm_home / ".codex-native/ownership.json").exists()
    assert not (home / ".codex/config.toml").exists()


def test_project_refusal_and_foreign_collision_do_not_cross_write(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Unsupported scope and foreign ownership fail without touching native settings."""
    codex = shutil.which("codex")
    assert codex is not None
    env = _environment(tmp_path)
    source = tmp_path / "source"
    _source(source, "INERT")
    reference = DependencyReference.parse(str(source))
    consumer = {
        "name": "consumer",
        "version": "1.0.0",
        "targets": ["codex"],
        "dependencies": {"apm": [str(source)]},
        "allowExecutables": {str(reference): {"mcp": True}},
    }
    dump_yaml(consumer, tmp_path / "apm.yml")
    _native_write(codex, env, tmp_path, {"model": "gpt-5.3-codex"})
    native_config = Path(env["CODEX_HOME"]) / "config.toml"
    before = native_config.read_bytes()
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    result = _apm(
        runner, ("install", "--target", "codex", "--no-policy"), tmp_path, env, "project-refusal"
    )
    assert result.returncode != 0
    assert "user scope" in result.stdout + result.stderr
    assert native_config.read_bytes() == before
    assert not list((tmp_path / ".agents").rglob("SKILL.md"))
    assert not any(path.is_file() for path in (tmp_path / ".codex").rglob("*"))

    apm_home = Path(env["APM_HOME"])
    apm_home.mkdir(exist_ok=True)
    dump_yaml(consumer, apm_home / "apm.yml")
    market = marketplace_name(apm_home / "apm_modules", str(reference))
    _native_write(codex, env, tmp_path, {f'plugins."example@{market}"': {"enabled": False}})
    before = native_config.read_bytes()
    result = _apm(
        runner,
        ("install", "-g", "--target", "codex", "--no-policy"),
        tmp_path,
        env,
        "foreign-collision",
    )
    assert result.returncode != 0
    assert "ownership collision" in result.stdout + result.stderr
    assert native_config.read_bytes() == before
    assert not (apm_home / ".codex-native/ownership.json").exists()
