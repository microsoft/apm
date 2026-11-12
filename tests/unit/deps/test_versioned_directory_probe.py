"""Real Git tree probes distinguish directories from files and symbolic links."""

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from apm_cli.deps import github_downloader_validation as validation
from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.models.dependency.reference import DependencyReference
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("directory-1.2.3", True),
        ("unknown.txt", False),
        ("file-1.2.3", False),
        ("link-1.2.3", False),
        ("submodule-1.2.3", False),
    ],
)
@pytest.mark.parametrize("api_body", [b'{"type":"file"}', b"invalid json"])
def test_subdirectory_probe_requires_a_git_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, expected: bool, api_body: bytes
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    repository = repositories.create("versioned-paths")
    directory = repository.worktree / "directory-1.2.3"
    directory.mkdir()
    (directory / "README.md").write_text("A directory", encoding="utf-8")
    for filename in ("unknown.txt", "file-1.2.3"):
        (repository.worktree / filename).write_text("A file", encoding="utf-8")
    if path == "link-1.2.3":
        try:
            (repository.worktree / path).symlink_to("directory-1.2.3", target_is_directory=True)
        except OSError:
            pytest.skip("Symbolic links are unavailable on this platform")
    commit = repositories.commit(repository, message="seed directory and file entries")
    if path == "submodule-1.2.3":
        (repository.worktree / path).mkdir()
        subprocess.run(
            [
                "git",
                "-C",
                str(repository.worktree),
                "update-index",
                "--add",
                "--cacheinfo",
                f"160000,{commit.sha},{path}",
            ],
            check=True,
            capture_output=True,
            env=environment,
        )
        commit = repositories.commit(repository, message="seed gitlink entry")
    monkeypatch.setattr(validation, "get_apm_temp_dir", lambda: isolated.temp_root)
    dependency = DependencyReference.parse_from_dict(
        {"git": "https://github.com/acme/catalog", "path": path, "ref": commit.sha}
    )
    attempt = validation.AttemptSpec("local fixture", repository.file_url, environment)
    downloader = GitHubPackageDownloader()
    downloader.auth_resolver = MagicMock()
    downloader.auth_resolver.uses_public_github_anonymous_first.return_value = False
    downloader.auth_resolver.resolve_for_dep.return_value = MagicMock(
        token=None, git_env=environment
    )
    response = requests.Response()
    response.status_code = 200
    response._content = api_body
    with (
        patch.object(downloader, "download_raw_file", side_effect=RuntimeError("404")),
        patch.object(downloader, "_resilient_get", return_value=response),
        patch.object(validation, "_build_validation_attempts", return_value=[attempt]),
    ):
        assert downloader.validate_virtual_package_exists(dependency) is expected
