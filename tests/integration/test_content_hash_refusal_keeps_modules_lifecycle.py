"""Installed-CLI contract: a content-hash refusal leaves apm_modules unchanged (#4353).

When the lockfile records a content hash the package source does not match,
``apm install`` exits 1 and names the mismatch. The refused fetch never
replaces or removes the copy already in ``apm_modules``, and the lockfile
bytes stay as they were (I7: a failed command preserves durable state).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I7", "P-LAW-transaction.failed_command_preserves_state"),
]

_REMOTE = "https://github.com/test/kit.git"


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.mark.parametrize("prior_copy", ["present", "absent"])
def test_content_hash_refusal_leaves_apm_modules_unchanged(
    tmp_path: Path, apm_binary_path: Path, prior_copy: str
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=env)
    source = isolated.package_root / "kit"
    (source / ".apm/skills/alpha").mkdir(parents=True)
    (source / "apm.yml").write_text("name: kit\nversion: 1.0.0\n", encoding="ascii")
    (source / ".apm/skills/alpha/SKILL.md").write_text(
        "---\nname: alpha\ndescription: Alpha\n---\nAlpha.\n", encoding="ascii"
    )
    repository = repositories.create("kit", source_tree=source)
    sha = repositories.commit(repository, message="v1").sha
    env = repositories.url_rewrite_subprocess_env(repository, _REMOTE)
    project = isolated.work_root / "consumer"
    project.mkdir()
    dump_yaml(
        {
            "name": "consumer",
            "version": "1.0.0",
            "targets": ["copilot"],
            "dependencies": {"apm": [{"git": _REMOTE, "ref": sha}]},
        },
        project / "apm.yml",
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
    first = runner.run(("install",), cwd=project, env=env)
    assert first.returncode == 0, first.stdout + first.stderr

    lock_path = project / "apm.lock.yaml"
    lock = lock_path.read_text(encoding="utf-8")
    # The first recorded content hash is the dependency row's package hash.
    tampered, count = re.subn(
        r"(?m)^  content_hash: sha256:[0-9a-f]{64}$",
        "  content_hash: sha256:" + "2" * 64,
        lock,
        count=1,
    )
    assert count == 1 and tampered.index("2" * 64) < tampered.index("\ndeployments:"), lock
    lock_path.write_text(tampered, encoding="utf-8")
    modules = project / "apm_modules"
    if prior_copy == "absent":
        import shutil

        shutil.rmtree(modules)
    before = _tree(modules) if modules.exists() else None
    deployed = _tree(project / ".agents")
    lock_before = lock_path.read_bytes()

    for _ in range(2):
        result = runner.run(("install",), cwd=project, env=env)
        output = " ".join((result.stdout + result.stderr).split())
        assert result.returncode == 1, output
        assert "Content hash mismatch for" in output, output
        assert lock_path.read_bytes() == lock_before
        assert (_tree(modules) if modules.exists() else None) == before
        assert _tree(project / ".agents") == deployed
