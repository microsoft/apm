"""Tests for apm deps list, tree, and info subcommands (pure logic cases)."""

import pytest

from apm_cli.commands.deps.cli import _deps_list_source_label

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-dependency-identity-materialization")]


def test_deps_list_source_label_mapping():
    """Source labels follow host class: local, ADO, GitLab, default GitHub bucket."""
    assert _deps_list_source_label(None, is_local=True) == "local"
    assert _deps_list_source_label(None, lockfile_source="local") == "local"
    assert _deps_list_source_label(None, lockfile_source="registry") == "registry"
    assert _deps_list_source_label("gitlab.com") == "gitlab"
    assert _deps_list_source_label("dev.azure.com") == "azure-devops"
    assert _deps_list_source_label("github.com") == "github"
    # GHES / generic enterprise: not GitLab-class → github bucket for this column
    assert _deps_list_source_label("ghes.example.org") == "github"
    # #3671: a declared or locked host_type labels a self-hosted GitLab.
    assert _deps_list_source_label("git.corp.example", host_type="gitlab") == "gitlab"
    assert _deps_list_source_label("git.corp.example") == "github"
