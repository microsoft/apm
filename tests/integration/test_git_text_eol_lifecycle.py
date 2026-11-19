"""APMLifecycle: host ``core.eol`` must not change locked package bytes (apm#3270).

A dependency repository sets ``* text=auto`` with LF-committed text, an
explicit ``eol=crlf`` script, a binary file and a CRLF blob committed before
the attributes. One host locks the package with the real installed CLI; a
fresh consumer under the opposite line-ending configuration replays that lock
with ``--frozen``. Both hosts must materialize the repository-defined bytes and
the same raw ``content_hash``.

The ``host-default`` producer sets no ``core.eol``: on Windows Git resolves it
to CRLF (``native``), so the Windows Compatibility Gate runs this module as
the native Windows control while POSIX lanes run the LF control.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from apm_cli.cache.paths import get_git_checkouts_path
from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.content_hash import compute_package_hash
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.windows_compat,
]

_INSTALL = ("install", "--no-policy", "--parallel-downloads", "0")
_REMOTE = "https://github.com/apm-fixture-org/eol-skills"
_ATTRIBUTES = b"* text=auto\n*.bat text eol=crlf\n*.bin binary\n"
_HOST_TEXT = ("apm.yml", ".apm/skills/demo/SKILL.md")
_PACKAGE: dict[str, bytes] = {
    "apm.yml": b"name: eol-pkg\nversion: 1.0.0\ndescription: EOL fixture\n",
    ".apm/skills/demo/SKILL.md": (
        b"---\nname: demo\ndescription: EOL fixture skill\n---\n# Demo\n\nLine one.\n"
    ),
    "scripts/run.bat": b"@echo off\r\necho hi\r\n",
    "assets/blob.bin": b"\x00\x01\r\n\x02\n",
    "legacy/committed-crlf.md": b"# Legacy\r\nkept as committed\r\n",
}
_LEGACY = "legacy/committed-crlf.md"
_DEPLOYED_SKILL = Path(".agents") / "skills" / "demo" / "SKILL.md"


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nreturncode={result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )


def _write(root: Path, rel: str, data: bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _host_env(base: dict[str, str], host: str, root: Path, cache: Path) -> dict[str, str]:
    """Return a consumer env with its own cache and host line-ending config."""
    env = dict(base)
    env["APM_CACHE_DIR"] = str(cache)
    if host.endswith("-global"):
        eol = host.split("-", 1)[0]
        config = root / f"{host}.gitconfig"
        original = Path(base["GIT_CONFIG_GLOBAL"]).read_bytes()
        config.write_bytes(original + f"[core]\n\teol = {eol}\n".encode("ascii"))
        env["GIT_CONFIG_GLOBAL"] = str(config)
    elif host == "crlf-env":
        index = int(env.get("GIT_CONFIG_COUNT", "0"))
        env["GIT_CONFIG_COUNT"] = str(index + 1)
        env[f"GIT_CONFIG_KEY_{index}"] = "core.eol"
        env[f"GIT_CONFIG_VALUE_{index}"] = "crlf"
    else:
        assert host == "host-default"
    return env


def _reference_hash(root: Path, prefix: str, *, expand_host_text: bool = False) -> str:
    """Hash the intended package bytes, or a pre-fix host-expanded CRLF variant."""
    for rel, data in _PACKAGE.items():
        if expand_host_text and rel in _HOST_TEXT:
            data = data.replace(b"\n", b"\r\n")
        _write(root, prefix + rel, data)
    if not prefix:
        _write(root, ".gitattributes", _ATTRIBUTES)
    return compute_package_hash(root / prefix if prefix else root)


def _installed_package(project: Path) -> Path:
    matches = [
        path.parent.parent
        for path in (project / "apm_modules").rglob("run.bat")
        if path.parent.name == "scripts"
    ]
    assert len(matches) == 1, matches
    return matches[0]


def _assert_intended_bytes(package_root: Path) -> None:
    for rel, data in _PACKAGE.items():
        assert (package_root / rel).read_bytes() == data, rel


def _locked(project: Path) -> dict[str, object]:
    dependencies = load_yaml(project / "apm.lock.yaml")["dependencies"]
    assert len(dependencies) == 1
    return dependencies[0]


def _cache_shards(cache: Path) -> list[Path]:
    root = get_git_checkouts_path(cache)
    return sorted({path.parent for path in root.rglob(".git") if path.is_dir()})


def _make_legacy_shard(shard: Path) -> None:
    """Rewrite a shard as a pre-#3270 Windows cache entry would look."""
    for skill in shard.rglob("SKILL.md"):
        skill.write_bytes(skill.read_bytes().replace(b"\n", b"\r\n"))
    config = shard / ".git" / "config"
    lines = config.read_text(encoding="utf-8").splitlines()
    config.write_text(
        "\n".join(line for line in lines if line.strip().lower() != "eol = lf") + "\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize("shape", ["subdirectory", "whole-repository"])
@pytest.mark.parametrize(
    ("lock_host", "frozen_host"),
    [("host-default", "crlf-global"), ("crlf-env", "lf-global")],
    ids=["default-to-crlf", "crlf-to-lf"],
)
def test_locked_text_auto_package_replays_across_host_eol(
    tmp_path_factory: pytest.TempPathFactory,
    apm_binary_path: Path,
    shape: str,
    lock_host: str,
    frozen_host: str,
) -> None:
    tmp_path = tmp_path_factory.mktemp("eol")
    isolated = IsolatedApmEnvironment.create(tmp_path / "iso", base_env=dict(os.environ))
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root, env=isolated.subprocess_env()
    )
    prefix = "pkg/" if shape == "subdirectory" else ""

    repository = repositories.create("eol-skills")
    _write(repository.worktree, prefix + _LEGACY, _PACKAGE[_LEGACY])
    repositories.commit(repository, message="legacy crlf before attributes")
    _write(repository.worktree, ".gitattributes", _ATTRIBUTES)
    for rel, data in _PACKAGE.items():
        if rel == "scripts/run.bat":
            data = data.replace(b"\r\n", b"\n")
        if rel != _LEGACY:
            _write(repository.worktree, prefix + rel, data)
    commit = repositories.commit(repository, message="text=auto package")
    base_env = repositories.url_rewrite_subprocess_env(repository, _REMOTE)

    dependency: dict[str, object] = {"git": _REMOTE, "ref": commit.sha}
    if prefix:
        dependency["path"] = prefix.rstrip("/")
    consumers = LocalPackageFactory(isolated.work_root)
    producer = consumers.create("producer", dependencies=(dependency,), targets=("copilot",)).root
    runner = ApmLifecycleRunner(
        (str(apm_binary_path),), timeout_seconds=120, scenario_timeout_seconds=900
    )
    expected_hash = _reference_hash(tmp_path / "reference", prefix)
    host_expanded_hash = _reference_hash(tmp_path / "host-expanded", prefix, expand_host_text=True)
    assert host_expanded_hash != expected_hash

    def run(project: Path, args: tuple[str, ...], env: dict[str, str], step: str) -> CommandResult:
        return runner.run(args, scenario_id=f"3270-{shape}-{step}", cwd=project, env=env)

    # Lock on the first host.
    lock_env = _host_env(base_env, lock_host, tmp_path, tmp_path / "cache-lock")
    result = run(producer, _INSTALL, lock_env, "lock")
    assert result.returncode == 0, _evidence(result)
    locked = _locked(producer)
    assert locked["resolved_commit"] == commit.sha
    assert locked["content_hash"] == expected_hash
    _assert_intended_bytes(_installed_package(producer))
    assert (producer / _DEPLOYED_SKILL).read_bytes() == _PACKAGE[".apm/skills/demo/SKILL.md"]

    # Fresh consumer under the opposite host configuration replays the lock.
    consumer = consumers.create("consumer", dependencies=(dependency,), targets=("copilot",)).root
    for name in ("apm.yml", "apm.lock.yaml"):
        shutil.copyfile(producer / name, consumer / name)
    manifest_bytes = (consumer / "apm.yml").read_bytes()
    lock_bytes = (consumer / "apm.lock.yaml").read_bytes()
    frozen_cache = tmp_path / "cache-frozen"
    frozen_env = _host_env(base_env, frozen_host, tmp_path, frozen_cache)
    for step in ("frozen-cold", "frozen-warm"):
        shutil.rmtree(consumer / "apm_modules", ignore_errors=True)
        result = run(consumer, (*_INSTALL, "--frozen"), frozen_env, step)
        assert result.returncode == 0, _evidence(result)
        assert (consumer / "apm.lock.yaml").read_bytes() == lock_bytes, step
        assert (consumer / "apm.yml").read_bytes() == manifest_bytes, step
        _assert_intended_bytes(_installed_package(consumer))
        assert (consumer / _DEPLOYED_SKILL).read_bytes() == _PACKAGE[".apm/skills/demo/SKILL.md"]

    # A cache shard written before the eol pin is rematerialized, not trusted.
    shards = _cache_shards(frozen_cache)
    assert shards
    for shard in shards:
        _make_legacy_shard(shard)
    shutil.rmtree(consumer / "apm_modules")
    result = run(consumer, (*_INSTALL, "--frozen"), frozen_env, "frozen-legacy-shard")
    assert result.returncode == 0, _evidence(result)
    assert (consumer / "apm.lock.yaml").read_bytes() == lock_bytes
    _assert_intended_bytes(_installed_package(consumer))
    for shard in _cache_shards(frozen_cache):
        assert b"\r\n" not in b"".join(p.read_bytes() for p in shard.rglob("SKILL.md"))
        assert "eol = lf" in (shard / ".git" / "config").read_text(encoding="utf-8")

    # A lock that recorded pre-fix host-expanded CRLF bytes still fails honestly.
    stale = load_yaml(consumer / "apm.lock.yaml")
    stale["dependencies"][0]["content_hash"] = host_expanded_hash
    dump_yaml(stale, consumer / "apm.lock.yaml")
    stale_bytes = (consumer / "apm.lock.yaml").read_bytes()
    shutil.rmtree(consumer / "apm_modules")
    result = run(consumer, (*_INSTALL, "--frozen"), frozen_env, "frozen-stale-hash")
    assert result.returncode != 0, _evidence(result)
    assert "Content hash mismatch" in result.stdout + result.stderr, _evidence(result)
    assert host_expanded_hash in result.stdout + result.stderr, _evidence(result)
    assert (consumer / "apm.lock.yaml").read_bytes() == stale_bytes
    assert (consumer / "apm.yml").read_bytes() == manifest_bytes

    # Deliberate non-frozen refresh re-records the intended hash at the same pin.
    result = run(consumer, (*_INSTALL, "--update"), frozen_env, "reviewed-refresh")
    assert result.returncode == 0, _evidence(result)
    refreshed = LockFile.read(consumer / "apm.lock.yaml")
    assert refreshed is not None
    assert _locked(consumer)["content_hash"] == expected_hash
    assert _locked(consumer)["resolved_commit"] == commit.sha
    result = run(consumer, (*_INSTALL, "--frozen"), frozen_env, "frozen-after-refresh")
    assert result.returncode == 0, _evidence(result)
