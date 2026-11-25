"""Hermetic e2e regression coverage for anonymous generic HTTPS clone URLs."""

from __future__ import annotations

import importlib
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
from tests.utils.checkout_trust import trust_checkout_contents
from tests.utils.git_link_fixture import git

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


def _resolved_main(commit: str) -> ResolvedReference:
    """Return a deterministic branch resolution with no network access."""
    return ResolvedReference(
        original_ref="main",
        ref_name="main",
        ref_type=GitReferenceType.BRANCH,
        resolved_commit=commit,
    )


@trust_checkout_contents()
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
    # The package the origin serves, at the commit admission resolves "main" to.
    served = tmp_path / "served"
    skill_dir = served / ".apm" / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (served / "apm.yml").write_text(
        "name: cloned-gitbucket-package\nversion: '1.0.0'\n",
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
    git("init", "-q", "-b", "main", "--template=", str(served))
    git("-C", str(served), "add", "-A")
    git("-C", str(served), "commit", "-q", "-m", "served")
    commit = git("-C", str(served), "rev-parse", "HEAD").strip()
    fetch_by_commit = importlib.import_module("apm_cli.deps.bare_cache").fetch_commit_worktree

    def capture_fetch(execute_transport_plan, repo_url_base, target, *, dep_ref, sha, **kw):
        """Run the real transport plan; the origin's fetch is served locally."""

        def plan(base, path, *, dep_ref, clone_action, **plan_kw):
            def serve(url: str, env: dict[str, str], dest: Path) -> None:
                captured["url"] = url
                captured["env"] = dict(env)
                captured["sha"] = sha
                clone_action(served.as_uri(), {}, dest)

            execute_transport_plan(base, path, dep_ref=dep_ref, clone_action=serve, **plan_kw)

        fetch_by_commit(plan, repo_url_base, target, dep_ref=dep_ref, sha=sha, **kw)

    monkeypatch.setattr(
        GitHubPackageDownloader,
        "resolve_git_reference",
        lambda self, repo_ref: _resolved_main(commit),
    )
    monkeypatch.setattr("apm_cli.deps.github_downloader.fetch_commit_worktree", capture_fetch)
    downloader = GitHubPackageDownloader()
    result = downloader.download_package(
        dep_ref,
        tmp_path / "apm_modules" / "owner" / "repo",
    )

    clone_url = captured["url"]
    assert isinstance(clone_url, str)
    parsed = urlparse(clone_url)
    assert parsed.scheme == "https"
    assert parsed.hostname == "gitbucket.example.com"
    assert parsed.username is None
    assert parsed.password is None
    assert parsed.path == "/owner/repo.git"
    # Fetched by the admitted commit, anonymously: no credential reaches Git.
    assert captured["sha"] == commit
    env = captured["env"]
    assert isinstance(env, dict)
    assert "GIT_HTTP_EXTRAHEADER" not in env
    entries = [
        (env[f"GIT_CONFIG_KEY_{index}"], env.get(f"GIT_CONFIG_VALUE_{index}", ""))
        for index in range(int(env.get("GIT_CONFIG_COUNT", "0")))
    ]
    # An empty extraheader only resets inherited headers; none may carry a value.
    assert all(not value for key, value in entries if "extraheader" in key.lower())
    assert not [value for _key, value in entries if "authorization" in value.lower()]
    assert result.package.name == "cloned-gitbucket-package"
