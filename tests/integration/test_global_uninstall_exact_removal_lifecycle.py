"""APMLifecycle: a global uninstall removes exactly what the global install deployed (#4248).

PROMISE: I5 -- each command writes only its declared write set and never
crosses scopes; I1 -- the lockfile matches the deployed files. A global
install that records its user-lock rows as ``scope: project`` (#4248) must
still remove only its own files on ``apm uninstall --global``.
CELL: OP=install,uninstall SRC=git PRIM=skill SCOPE=user TGT=claude
CLAUDE_CONFIG_DIR=default|outside-home
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I5", "P-INV-I1"),
]

_REMOTE = "https://gitlab.example.invalid/acme/model-skill.git"
_SKILL = "---\nname: model-skill\ndescription: model skill\n---\n# model skill\n"


def _tree(roots: dict[str, Path]) -> dict[str, bytes]:
    """Every file and symlink (dangling ones included) under each root, without following links."""
    tree: dict[str, bytes] = {}
    for label, root in roots.items():
        if not root.exists():
            continue
        for directory, dirnames, filenames in os.walk(root, followlinks=False):
            base = Path(directory)
            for name in (*dirnames, *filenames):
                path = base / name
                key = f"{label}:{path.relative_to(root).as_posix()}"
                if path.is_symlink():
                    tree[key] = b"symlink -> " + os.fsencode(os.readlink(path))
                elif path.is_file():
                    tree[key] = path.read_bytes()
    return tree


@pytest.mark.parametrize("layout", ["default", "claude-config-dir-outside-home"])
def test_global_uninstall_removes_exactly_the_installed_files(
    tmp_path: Path, apm_binary_path: Path, layout: str
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    if layout == "default":
        env.pop("CLAUDE_CONFIG_DIR", None)
        claude_root = isolated.home / ".claude"
    else:
        claude_root = isolated.root / "claude-home"
        env["CLAUDE_CONFIG_DIR"] = str(claude_root)

    source = isolated.package_root / "model-skill"
    skill = source / ".apm" / "skills" / "model-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(_SKILL, encoding="utf-8")
    dump_yaml({"name": "model-skill", "version": "1.0.0"}, source / "apm.yml")
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=env)
    repository = repositories.create("model-skill", source_tree=source)
    commit = repositories.commit(repository, message="seed model-skill")
    env.update(repositories.url_rewrite_subprocess_env(repository, _REMOTE))

    dump_yaml(
        {
            "name": "user",
            "version": "1.0.0",
            "targets": ["claude"],
            "dependencies": {"apm": [{"git": _REMOTE, "type": "gitlab", "ref": commit.sha}]},
        },
        isolated.config_root / "apm.yml",
    )
    # Unrelated user content under the same skills root and a sibling under the target root.
    unrelated = claude_root / "skills" / "my-own-skill" / "SKILL.md"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text(
        "---\nname: my-own-skill\ndescription: mine\n---\n# mine\n", encoding="utf-8"
    )
    sibling = claude_root / "settings.local.json"
    sibling.write_text('{"keep": true}\n', encoding="utf-8")

    roots = {"home": isolated.home, "apm_home": isolated.config_root, "claude": claude_root}
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=900)

    def apm(*args: str, step: str) -> None:
        result = runner.run(args, scenario_id=step, cwd=isolated.work_root, env=env)
        assert result.returncode == 0, result.stdout + result.stderr

    before = _tree(roots)
    apm("install", "--no-policy", "--global", step="1-install-global")
    installed = _tree(roots)
    deployed = {
        key
        for key in installed
        if key.startswith("claude:skills/model-skill/") and key not in before
    }
    assert deployed, sorted(set(installed) - set(before))
    lock = load_yaml(isolated.config_root / "apm.lock.yaml")
    assert [row["repo_url"] for row in lock["dependencies"]] == ["acme/model-skill"]
    assert lock["deployments"], lock

    apm("uninstall", "--global", _REMOTE, step="2-uninstall-global")
    after = _tree(roots)

    assert not set(deployed) & set(after), sorted(set(deployed) & set(after))
    # The manifest is uninstall's declared write: it drops the dependency.
    manifests = {"apm_home:apm.yml", "home:.apm/apm.yml"}
    assert not (load_yaml(isolated.config_root / "apm.yml")["dependencies"].get("apm") or [])
    for key, data in before.items():
        if key not in manifests:
            assert after.get(key) == data, key
    assert (
        unrelated.read_text(encoding="utf-8")
        == "---\nname: my-own-skill\ndescription: mine\n---\n# mine\n"
    )
    assert sibling.read_text(encoding="utf-8") == '{"keep": true}\n'
    # Outside APM's own state (APM_HOME, ~/.apm-local) uninstall leaves nothing new in any root.
    apm_state = ("apm_home:", "home:.apm/", "home:.apm-local/")
    leftover = sorted(key for key in set(after) - set(before) if not key.startswith(apm_state))
    assert leftover == [], leftover
    lock_path = isolated.config_root / "apm.lock.yaml"
    lock_after = (load_yaml(lock_path) or {}) if lock_path.exists() else {}
    assert not (lock_after.get("dependencies") or []), lock_after
    assert not (lock_after.get("deployments") or []), lock_after
