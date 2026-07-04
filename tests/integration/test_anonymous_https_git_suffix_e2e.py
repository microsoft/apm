"""Hermetic e2e regression coverage for anonymous generic HTTPS clone URLs."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import pytest

from apm_cli.core.token_manager import GitHubTokenManager
from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.models.apm_package import (
    APMPackage,
    GitReferenceType,
    ResolvedReference,
    clear_apm_yml_cache,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_e2e_mode,
]


class _FakeGit:
    """Minimal GitPython ``repo.git`` stand-in used by downloader cleanup."""

    def clear_cache(self) -> None:
        """Match the GitPython cleanup surface."""


class _FakeRepo:
    """Minimal GitPython repo stand-in returned by the fake clone."""

    git = _FakeGit()

    def close(self) -> None:
        """Match the GitPython cleanup surface."""


def _write_manifest(project: Path) -> Path:
    """Write an apm.yml that keeps a .git suffix in user input."""
    apm_yml = project / "apm.yml"
    apm_yml.write_text(
        "\n".join(
            [
                "name: gitbucket-repro",
                "version: '1.0.0'",
                "dependencies:",
                "  apm:",
                "    - git: https://gitbucket.example.com/owner/repo.git",
                "      ref: main",
                "    ",
                "  mcp: []",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return apm_yml


def _resolved_main() -> ResolvedReference:
    """Return a deterministic branch resolution with no network access."""
    return ResolvedReference(
        original_ref="main",
        ref_name="main",
        ref_type=GitReferenceType.BRANCH,
        resolved_commit="a" * 40,
    )


def test_generic_anonymous_https_clone_url_keeps_git_suffix_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A GitBucket-style generic HTTPS dependency clones with /owner/repo.git."""
    for name in (
        "GITHUB_APM_PAT",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "ADO_APM_PAT",
        "GITLAB_APM_PAT",
        "GITLAB_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        GitHubTokenManager,
        "resolve_credential_from_git",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        GitHubTokenManager,
        "resolve_credential_from_gh_cli",
        lambda *args, **kwargs: None,
    )

    clear_apm_yml_cache()
    apm_yml = _write_manifest(tmp_path)
    package = APMPackage.from_apm_yml(apm_yml)
    dep_ref = package.get_apm_dependencies()[0]
    captured: dict[str, object] = {}

    def fake_clone_from(
        url: str,
        target: Path,
        *,
        env: dict[str, str] | None = None,
        progress: object | None = None,
        **kwargs: object,
    ) -> _FakeRepo:
        """Capture the clone URL and materialise a valid package."""
        captured["url"] = url
        captured["env"] = env or {}
        captured["kwargs"] = kwargs
        target.mkdir(parents=True, exist_ok=True)
        skill_dir = target / ".apm" / "skills" / "demo"
        skill_dir.mkdir(parents=True)
        (target / "apm.yml").write_text(
            "name: cloned-gitbucket-package\nversion: '1.0.0'\n",
            encoding="utf-8",
        )
        (skill_dir / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
        return _FakeRepo()

    monkeypatch.setattr("apm_cli.deps.github_downloader.Repo.clone_from", fake_clone_from)
    monkeypatch.setattr(
        GitHubPackageDownloader,
        "resolve_git_reference",
        lambda self, repo_ref: _resolved_main(),
    )

    downloader = GitHubPackageDownloader()
    result = downloader.download_package(dep_ref, tmp_path / "apm_modules" / "owner" / "repo")

    clone_url = captured["url"]
    assert isinstance(clone_url, str)
    parsed = urlparse(clone_url)
    assert parsed.scheme == "https"
    assert parsed.hostname == "gitbucket.example.com"
    assert parsed.username is None
    assert parsed.password is None
    assert parsed.path == "/owner/repo.git"
    assert captured["kwargs"] == {"depth": 1, "branch": "main"}
    assert result.package.name == "cloned-gitbucket-package"
