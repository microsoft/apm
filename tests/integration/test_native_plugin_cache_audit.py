"""Installed-CLI regression traps for opaque native plugin cache integrity."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sysconfig
from pathlib import Path

import pytest

from apm_cli.native_plugins.binding import project_marketplace_name
from tests.unit.copilot_plugins._builders import write_agent_plugin
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


def _snapshot(root: Path) -> dict[str, object]:
    """Include file bytes, permissions and timestamps in the read-only witness."""
    result: dict[str, object] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[relative] = ("symlink", os.readlink(path))
        elif path.is_file():
            stat = path.stat()
            result[relative] = (
                hashlib.sha256(path.read_bytes()).hexdigest(),
                stat.st_mode,
                stat.st_mtime_ns,
            )
    return result


@pytest.mark.parametrize("copy", ["source", "deployed"])
@pytest.mark.parametrize("with_manifest", [False, True])
@pytest.mark.parametrize("tamper", ["modified", "missing"])
@pytest.mark.parametrize("source_kind", ["local", "git"])
def test_native_cache_tamper_fails_installed_audit(
    tmp_path: Path, with_manifest: bool, tamper: str, source_kind: str, copy: str
) -> None:
    """Clean/reinstall pass; drift in either plugin copy fails without changing any input.

    ``source`` is the fetched package in ``apm_modules``; ``deployed`` is the
    copy under ``.agents/plugins`` that Copilot loads.
    """
    project, source, home, scratch = (
        tmp_path / name for name in ("project", "source", "home", "scratch")
    )
    for root in (project, home, scratch):
        root.mkdir()
    name = "cache-witness"
    write_agent_plugin(source, name=name, with_mcp=False)
    if with_manifest:
        (source / "apm.yml").write_text(
            json.dumps({"name": name, "version": "1.0.0"}), encoding="ascii"
        )
    executable = Path(sysconfig.get_path("scripts")) / ("apm.exe" if os.name == "nt" else "apm")
    assert executable.is_file(), "The test requires the installed project CLI"
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    env = {
        **isolated.subprocess_env(),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "COPILOT_HOME": str(home / ".copilot"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "TMPDIR": str(scratch),
        "TMP": str(scratch),
        "TEMP": str(scratch),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    dependency: str | dict[str, str] = str(source)
    if source_kind == "git":
        repositories = LocalGitRepositoryFactory(tmp_path / "repositories", env=env)
        repository = repositories.create("native-witness", source_tree=source)
        commit = repositories.commit(repository, message="Seed harmless native fixture")
        remote_url = "https://github.com/test/native-witness.git"
        env = repositories.url_rewrite_subprocess_env(repository, remote_url)
        dependency = {"git": remote_url, "ref": commit.sha}
    (project / "apm.yml").write_text(
        json.dumps(
            {
                "name": "consumer",
                "version": "1.0.0",
                "targets": ["copilot"],
                "dependencies": {"apm": [dependency]},
            }
        ),
        encoding="ascii",
    )

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(executable), *args],
            cwd=project,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def audit() -> subprocess.CompletedProcess[str]:
        protected = (project, source, home, isolated.root)
        before = [_snapshot(root) for root in protected]
        result = run("audit", "--no-policy", "--ci")
        assert [_snapshot(root) for root in protected] == before
        return result

    for _ in range(2):
        installed = run("install", "--no-policy", "--target", "copilot")
        assert installed.returncode == 0, installed.stdout + installed.stderr
        clean = audit()
        assert clean.returncode == 0, clean.stdout + clean.stderr
        assert "no drift detected against lockfile" in clean.stdout

    plugins = project / ".agents" / "plugins"
    marketplace = json.loads((plugins / ".claude-plugin/marketplace.json").read_text("utf-8"))
    assert marketplace["name"] == project_marketplace_name("consumer")
    assert marketplace["plugins"] == [{"name": name, "source": f"./{name}"}]
    settings_path = project / ".github/copilot/settings.json"
    settings = settings_path.read_bytes()
    relative = Path("skills") / f"{name}-skill" / "SKILL.md"
    original = (source / relative).read_bytes()
    modules = project / "apm_modules"
    (materialized,) = (path.parents[2] for path in modules.rglob(relative.as_posix()))
    assert (materialized / relative).read_bytes() == original
    skill = (materialized if copy == "source" else plugins / name) / relative
    assert skill.read_bytes() == original
    assert not (project / ".agents/skills" / f"{name}-skill").exists()
    if tamper == "modified":
        skill.write_bytes(original + b"\nModified only in one native copy.\n")
    else:
        skill.unlink()

    changed = audit()
    assert changed.returncode != 0, changed.stdout + changed.stderr
    message = " ".join(changed.stdout.split())
    if copy == "deployed":
        assert f"Native plugin '{name}' deployed copy differs from apm.lock.yaml" in message
        assert "Run 'apm install' to restore it." in message
    else:
        assert "Native Agent Plugin cache differs from its replay baseline" in message
        assert "run 'apm install' to restore the package" in message
    assert "Traceback" not in changed.stderr
    assert settings_path.read_bytes() == settings
    assert (source / relative).read_bytes() == original
    restored = run("install", "--no-policy", "--target", "copilot")
    assert restored.returncode == 0, restored.stdout + restored.stderr
    assert skill.read_bytes() == original
    clean = audit()
    assert clean.returncode == 0, clean.stdout + clean.stderr
