"""Tests for apm deps list, tree, and info subcommands (pure logic cases)."""

import pytest

from apm_cli.commands.deps.cli import _deps_list_source_label

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-dependency-identity-materialization")]


def test_deps_list_source_label_mapping(monkeypatch):
    """Source labels follow host class: local, ADO, GitLab, GitHub, generic git."""
    monkeypatch.delenv("GITHUB_HOST", raising=False)
    assert _deps_list_source_label(None, is_local=True) == "local"
    assert _deps_list_source_label(None, lockfile_source="local") == "local"
    assert _deps_list_source_label(None, lockfile_source="registry") == "registry"
    assert _deps_list_source_label("gitlab.com") == "gitlab"
    assert _deps_list_source_label("dev.azure.com") == "azure-devops"
    assert _deps_list_source_label("github.com") == "github"
    # #3671: a declared or locked host_type labels a self-hosted GitLab.
    assert _deps_list_source_label("git.corp.example", host_type="gitlab") == "gitlab"
    # RA-019: an unrecognized host is a plain git host, not GitHub.
    assert _deps_list_source_label("git.corp.example") == "git"
    assert _deps_list_source_label("git.example.invalid:8443") == "git"
    # A configured GHES host stays in the github bucket.
    monkeypatch.setenv("GITHUB_HOST", "ghes.example.org")
    assert _deps_list_source_label("ghes.example.org") == "github"
