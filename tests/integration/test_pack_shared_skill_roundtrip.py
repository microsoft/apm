"""Hermetic install -> APM ZIP -> restore proof for split-root target layouts."""

from __future__ import annotations

import hashlib
import os
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.deps.lockfile import LockFile
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = pytest.mark.component


def test_install_pack_zip_restore_preserves_shared_skill(tmp_path: Path) -> None:
    """Pack installed bytes (including nested assets) and their real install hashes."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=os.environ)
    packages = LocalPackageFactory(isolated.package_root)
    source = packages.create("review-kit", targets=("copilot",))
    skill = packages.add_skill(
        source,
        "review",
        "---\nname: review\ndescription: Review code using the bundled rules\n---\n"
        "# Review\nRead [rules](assets/rules.json).\n",
    )
    asset = skill.parent / "assets" / "rules.json"
    asset.parent.mkdir()
    asset.write_bytes(b'{"rules": ["check tests"]}\n')
    packages.add_agent(
        source, "reviewer", "---\ndescription: Review changes\n---\n# Reviewer\nCheck tests.\n"
    )
    packages.add_instruction(
        source, "style", "---\napplyTo: '**/*.py'\n---\n# Style\nUse type hints.\n"
    )
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root, env=isolated.subprocess_env()
    )
    repository = repositories.create(source.name, source_tree=source.root)
    repositories.commit(repository, message="seed pack roundtrip fixture")
    remote = "https://github.com/fixture/review-kit"
    environment = repositories.url_rewrite_subprocess_env(repository, remote)
    projects = LocalPackageFactory(isolated.work_root)
    producer = projects.create("producer", dependencies=({"git": remote},), targets=("copilot",))
    consumer = projects.create("consumer", targets=("copilot",))
    runner = CliRunner()

    # Only local file:// Git transport is permitted; any attempted HTTP is denied.
    # Keep the actual resolver, integrators, hashing, packer and restore path real.
    with (
        patch.dict(os.environ, environment, clear=True),
        patch("requests.sessions.Session.request", side_effect=OSError("HTTP disabled in test")),
        pytest.MonkeyPatch.context() as monkeypatch,
    ):
        monkeypatch.chdir(producer.root)
        installed = runner.invoke(cli, ["install", "--target", "copilot", "--no-policy"])
        assert installed.exit_code == 0, installed.output
        lockfile = LockFile.read(producer.root / "apm.lock.yaml")
        assert lockfile is not None
        dependencies = lockfile.get_all_dependencies()
        assert len(dependencies) == 1
        dependency = dependencies[0]
        assert dependency.source != "local"
        skill_dir = ".agents/skills/review/"
        assert skill_dir.rstrip("/") in dependency.deployed_files
        hashes = dependency.deployed_file_hashes
        expected_files = {path: (producer.root / path).read_bytes() for path in hashes}
        assert {f"{skill_dir}SKILL.md", f"{skill_dir}assets/rules.json"} <= expected_files.keys()
        assert any(path.startswith(".github/agents/") for path in expected_files)
        assert any(path.startswith(".github/instructions/") for path in expected_files)
        for path, content in expected_files.items():
            assert hashes[path] == f"sha256:{hashlib.sha256(content).hexdigest()}"

        packed = runner.invoke(cli, ["pack", "--format", "apm", "--archive", "--target", "copilot"])
        assert packed.exit_code == 0, packed.output
        output = " ".join(packed.output.split())
        assert "Legacy APM bundles still filter paths by target" in output
        assert "Share with: apm unpack" in output
        archives = list((producer.root / "build").glob("*.zip"))
        assert len(archives) == 1
        with zipfile.ZipFile(archives[0]) as archive:
            lock_names = [name for name in archive.namelist() if name.endswith("/apm.lock.yaml")]
            assert len(lock_names) == 1
            prefix = lock_names[0].removesuffix("apm.lock.yaml")
            packed_lock = yaml.safe_load(archive.read(lock_names[0]))
            packed_dep = packed_lock["dependencies"][0]
            assert skill_dir.rstrip("/") in packed_dep["deployed_files"]
            assert packed_dep["deployed_file_hashes"] == hashes
            for path, content in expected_files.items():
                assert archive.read(prefix + path) == content

        monkeypatch.chdir(consumer.root)
        # APM-format archives use unpack's project-relative restore path;
        # imperative `install <zip>` accepts plugin-format bundles only.
        restored = runner.invoke(cli, ["unpack", str(archives[0])])
        assert restored.exit_code == 0, restored.output
        for path, content in expected_files.items():
            restored_content = (consumer.root / path).read_bytes()
            assert restored_content == content
            assert f"sha256:{hashlib.sha256(restored_content).hexdigest()}" == hashes[path]
