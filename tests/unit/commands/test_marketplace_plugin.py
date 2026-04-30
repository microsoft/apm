"""Tests for ``apm marketplace package {add,set,remove}`` CLI commands."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch  # noqa: F401

import pytest
from click.testing import CliRunner

from apm_cli.commands.marketplace import marketplace
from apm_cli.commands.marketplace.plugin import _SHA_RE, _resolve_ref  # noqa: F401
from apm_cli.core.command_logger import CommandLogger
from apm_cli.marketplace.ref_resolver import RemoteRef

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yml(tmp_path: Path, content: str | None = None) -> Path:
    """Scaffold a valid ``marketplace.yml`` in *tmp_path*."""
    if content is None:
        content = textwrap.dedent("""\
            name: test-marketplace
            description: Test marketplace
            version: 1.0.0
            owner:
              name: Test Owner
            packages:
              - name: existing-package
                source: acme/existing-package
                version: ">=1.0.0"
                description: An existing package
        """)
    p = tmp_path / "marketplace.yml"
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner():
    return CliRunner()


# ---------------------------------------------------------------------------
# package add
# ---------------------------------------------------------------------------


class TestPackageAdd:
    def test_happy_path_no_verify(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(
            marketplace,
            [
                "package",
                "add",
                "acme/new-tool",
                "--version",
                ">=2.0.0",
                "--no-verify",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "new-tool" in result.output

    def test_duplicate_name_exits_2(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(
            marketplace,
            [
                "package",
                "add",
                "acme/existing-package",
                "--version",
                ">=1.0.0",
                "--no-verify",
            ],
        )
        assert result.exit_code == 2
        assert "already exists" in result.output

    def test_missing_version_and_ref_no_verify_exits_2(self, runner, tmp_path, monkeypatch):
        """With --no-verify and no --ref/--version, auto-resolve fails."""
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(
            marketplace,
            ["package", "add", "acme/tool", "--no-verify"],
        )
        assert result.exit_code == 2
        assert "Cannot resolve HEAD" in result.output

    def test_version_and_ref_conflict_exits_2(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(
            marketplace,
            [
                "package",
                "add",
                "acme/tool",
                "--version",
                ">=1.0.0",
                "--ref",
                "abc",
                "--no-verify",
            ],
        )
        assert result.exit_code == 2
        assert "mutually exclusive" in result.output.lower()

    def test_help_renders(self, runner):
        result = runner.invoke(marketplace, ["package", "add", "--help"])
        assert result.exit_code == 0
        assert "Add a package" in result.output

    def test_verify_calls_ref_resolver(self, runner, tmp_path, monkeypatch):
        """Without --no-verify the command calls list_remote_refs."""
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        monkeypatch.setattr(
            "apm_cli.marketplace.ref_resolver.RefResolver.list_remote_refs",
            lambda self, source: [],
        )
        result = runner.invoke(
            marketplace,
            [
                "package",
                "add",
                "acme/verified-tool",
                "--version",
                ">=1.0.0",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "verified-tool" in result.output


# ---------------------------------------------------------------------------
# package set
# ---------------------------------------------------------------------------


class TestPackageSet:
    def test_happy_path_update_version(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(
            marketplace,
            [
                "package",
                "set",
                "existing-package",
                "--version",
                ">=2.0.0",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Updated" in result.output

    def test_package_not_found_exits_2(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(
            marketplace,
            [
                "package",
                "set",
                "nonexistent",
                "--version",
                ">=1.0.0",
            ],
        )
        assert result.exit_code == 2
        assert "not found" in result.output

    def test_help_renders(self, runner):
        result = runner.invoke(marketplace, ["package", "set", "--help"])
        assert result.exit_code == 0
        assert "Update a package" in result.output

    def test_version_and_ref_conflict_exits_2(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(
            marketplace,
            [
                "package",
                "set",
                "existing-package",
                "--version",
                ">=2.0.0",
                "--ref",
                "abc",
            ],
        )
        assert result.exit_code == 2
        assert "mutually exclusive" in result.output.lower()

    def test_set_no_fields_errors(self, runner, tmp_path, monkeypatch):
        """Calling ``package set`` with no field flags produces an error."""
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(
            marketplace,
            ["package", "set", "existing-package"],
        )
        assert result.exit_code == 1
        assert "No fields specified" in result.output


# ---------------------------------------------------------------------------
# package remove
# ---------------------------------------------------------------------------


class TestPackageRemove:
    def test_happy_path_with_yes(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(
            marketplace,
            ["package", "remove", "existing-package", "--yes"],
        )
        assert result.exit_code == 0, result.output
        assert "Removed" in result.output

    def test_without_yes_non_interactive_cancels(self, runner, tmp_path, monkeypatch):
        """Non-interactive mode exits with error asking for --yes."""
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(
            marketplace,
            ["package", "remove", "existing-package"],
        )
        # Non-interactive guard fires: exit 1 with guidance to use --yes.
        assert result.exit_code == 1
        assert "--yes" in result.output

    def test_package_not_found_exits_2(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(
            marketplace,
            ["package", "remove", "nonexistent", "--yes"],
        )
        assert result.exit_code == 2
        assert "not found" in result.output

    def test_help_renders(self, runner):
        result = runner.invoke(marketplace, ["package", "remove", "--help"])
        assert result.exit_code == 0
        assert "Remove a package" in result.output


# ---------------------------------------------------------------------------
# UX4: --version/--ref mutual exclusivity in package add
# ---------------------------------------------------------------------------


class TestPackageAddMutualExclusivity:
    """The ``add`` command must reject ``--version`` and ``--ref`` together."""

    def test_version_and_ref_mutually_exclusive(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(
            marketplace,
            [
                "package",
                "add",
                "acme/new-tool",
                "--version",
                "1.0.0",
                "--ref",
                "main",
                "--no-verify",
            ],
        )
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output.lower()


# ---------------------------------------------------------------------------
# _resolve_ref unit tests
# ---------------------------------------------------------------------------

_FAKE_SHA = "a" * 40
_FAKE_SHA_B = "b" * 40


class TestResolveRef:
    """Unit tests for the ``_resolve_ref()`` helper."""

    def _make_logger(self) -> CommandLogger:
        return CommandLogger("test", verbose=False)

    def test_version_set_returns_none(self):
        """When version is provided, ref resolution is skipped."""
        result = _resolve_ref(
            self._make_logger(),
            "acme/tools",
            ref=None,
            version=">=1.0.0",
            no_verify=False,
        )
        assert result is None

    def test_no_ref_no_verify_exits(self):
        """No --ref, --no-verify → error (cannot resolve without network)."""
        with pytest.raises(SystemExit) as exc_info:
            _resolve_ref(
                self._make_logger(),
                "acme/tools",
                ref=None,
                version=None,
                no_verify=True,
            )
        assert exc_info.value.code == 2

    @patch(
        "apm_cli.marketplace.ref_resolver.RefResolver.resolve_ref_sha",
        return_value=_FAKE_SHA,
    )
    def test_no_ref_resolves_head(self, mock_resolve):
        """No --ref, no --version → auto-resolve HEAD."""
        result = _resolve_ref(
            self._make_logger(),
            "acme/tools",
            ref=None,
            version=None,
            no_verify=False,
        )
        assert result == _FAKE_SHA
        mock_resolve.assert_called_once_with("acme/tools", "HEAD")

    def test_explicit_head_no_verify_exits(self):
        """--ref HEAD + --no-verify → error."""
        with pytest.raises(SystemExit) as exc_info:
            _resolve_ref(
                self._make_logger(),
                "acme/tools",
                ref="HEAD",
                version=None,
                no_verify=True,
            )
        assert exc_info.value.code == 2

    @patch(
        "apm_cli.marketplace.ref_resolver.RefResolver.resolve_ref_sha",
        return_value=_FAKE_SHA,
    )
    def test_explicit_head_resolves(self, mock_resolve):
        """--ref HEAD → warn + resolve."""
        result = _resolve_ref(
            self._make_logger(),
            "acme/tools",
            ref="HEAD",
            version=None,
            no_verify=False,
        )
        assert result == _FAKE_SHA

    @patch(
        "apm_cli.marketplace.ref_resolver.RefResolver.resolve_ref_sha",
        return_value=_FAKE_SHA,
    )
    def test_explicit_head_case_insensitive(self, mock_resolve):
        """--ref head (lowercase) → treated as HEAD."""
        result = _resolve_ref(
            self._make_logger(),
            "acme/tools",
            ref="head",
            version=None,
            no_verify=False,
        )
        assert result == _FAKE_SHA

    def test_sha_returns_as_is(self):
        """A 40-char hex SHA is returned unchanged."""
        result = _resolve_ref(
            self._make_logger(),
            "acme/tools",
            ref=_FAKE_SHA,
            version=None,
            no_verify=False,
        )
        assert result == _FAKE_SHA

    @patch(
        "apm_cli.marketplace.ref_resolver.RefResolver.list_remote_refs",
        return_value=[
            RemoteRef(name="refs/heads/main", sha=_FAKE_SHA_B),
            RemoteRef(name="refs/tags/v1.0.0", sha=_FAKE_SHA),
        ],
    )
    def test_branch_name_resolves_to_sha(self, mock_list):
        """--ref main (matching refs/heads/main) → warn + resolve."""
        result = _resolve_ref(
            self._make_logger(),
            "acme/tools",
            ref="main",
            version=None,
            no_verify=False,
        )
        assert result == _FAKE_SHA_B

    @patch(
        "apm_cli.marketplace.ref_resolver.RefResolver.list_remote_refs",
        return_value=[
            RemoteRef(name="refs/heads/main", sha=_FAKE_SHA),
            RemoteRef(name="refs/tags/v1.0.0", sha=_FAKE_SHA_B),
        ],
    )
    def test_tag_name_returns_as_is(self, mock_list):
        """--ref v1.0.0 (not a branch) → returned as-is."""
        result = _resolve_ref(
            self._make_logger(),
            "acme/tools",
            ref="v1.0.0",
            version=None,
            no_verify=False,
        )
        assert result == "v1.0.0"


# ---------------------------------------------------------------------------
# Integration: package add with ref auto-resolution
# ---------------------------------------------------------------------------


class TestPackageAddRefResolution:
    """Integration tests for ref auto-resolution in ``package add``."""

    @patch(
        "apm_cli.marketplace.ref_resolver.RefResolver.resolve_ref_sha",
        return_value=_FAKE_SHA,
    )
    @patch(
        "apm_cli.marketplace.ref_resolver.RefResolver.list_remote_refs",
        return_value=[],
    )
    def test_add_no_ref_auto_resolves_head(
        self,
        mock_list,
        mock_resolve,
        runner,
        tmp_path,
        monkeypatch,
    ):
        """``package add <source>`` (no --ref, no --version) pins HEAD SHA."""
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(
            marketplace,
            ["package", "add", "acme/new-tool"],
        )
        assert result.exit_code == 0, result.output
        assert "new-tool" in result.output
        # Verify the SHA was stored in marketplace.yml.
        yml_content = (tmp_path / "marketplace.yml").read_text()
        assert _FAKE_SHA in yml_content

    @patch(
        "apm_cli.marketplace.ref_resolver.RefResolver.resolve_ref_sha",
        return_value=_FAKE_SHA,
    )
    @patch(
        "apm_cli.marketplace.ref_resolver.RefResolver.list_remote_refs",
        return_value=[],
    )
    def test_add_ref_head_warns_and_resolves(
        self,
        mock_list,
        mock_resolve,
        runner,
        tmp_path,
        monkeypatch,
    ):
        """``package add <source> --ref HEAD`` warns + stores SHA."""
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(
            marketplace,
            ["package", "add", "acme/new-tool", "--ref", "HEAD"],
        )
        assert result.exit_code == 0, result.output
        assert "mutable ref" in result.output
        yml_content = (tmp_path / "marketplace.yml").read_text()
        assert _FAKE_SHA in yml_content

    @patch(
        "apm_cli.marketplace.ref_resolver.RefResolver.list_remote_refs",
        return_value=[
            RemoteRef(name="refs/heads/main", sha=_FAKE_SHA),
        ],
    )
    def test_add_ref_branch_warns_and_resolves(
        self,
        mock_list,
        runner,
        tmp_path,
        monkeypatch,
    ):
        """``package add <source> --ref main`` warns + stores branch SHA."""
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(
            marketplace,
            ["package", "add", "acme/new-tool", "--ref", "main"],
        )
        assert result.exit_code == 0, result.output
        assert "mutable ref" in result.output
        yml_content = (tmp_path / "marketplace.yml").read_text()
        assert _FAKE_SHA in yml_content

    def test_add_ref_sha_stores_as_is(
        self,
        runner,
        tmp_path,
        monkeypatch,
    ):
        """``package add <source> --ref <sha>`` stores SHA directly."""
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(
            marketplace,
            [
                "package",
                "add",
                "acme/new-tool",
                "--ref",
                _FAKE_SHA,
                "--no-verify",
            ],
        )
        assert result.exit_code == 0, result.output
        yml_content = (tmp_path / "marketplace.yml").read_text()
        assert _FAKE_SHA in yml_content


# ---------------------------------------------------------------------------
# Integration: package set with ref auto-resolution
# ---------------------------------------------------------------------------


class TestPackageSetRefResolution:
    """Integration tests for ref auto-resolution in ``package set``."""

    @patch(
        "apm_cli.marketplace.ref_resolver.RefResolver.resolve_ref_sha",
        return_value=_FAKE_SHA,
    )
    @patch(
        "apm_cli.marketplace.ref_resolver.RefResolver.list_remote_refs",
        return_value=[],
    )
    def test_set_ref_head_resolves(
        self,
        mock_list,
        mock_resolve,
        runner,
        tmp_path,
        monkeypatch,
    ):
        """``package set <name> --ref HEAD`` resolves to SHA."""
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(
            marketplace,
            ["package", "set", "existing-package", "--ref", "HEAD"],
        )
        assert result.exit_code == 0, result.output
        assert "Updated" in result.output

    @patch(
        "apm_cli.marketplace.ref_resolver.RefResolver.list_remote_refs",
        return_value=[
            RemoteRef(name="refs/heads/develop", sha=_FAKE_SHA_B),
        ],
    )
    def test_set_ref_branch_resolves(
        self,
        mock_list,
        runner,
        tmp_path,
        monkeypatch,
    ):
        """``package set <name> --ref develop`` resolves branch to SHA."""
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(
            marketplace,
            ["package", "set", "existing-package", "--ref", "develop"],
        )
        assert result.exit_code == 0, result.output
        assert "Updated" in result.output

    def test_set_ref_sha_stores_directly(
        self,
        runner,
        tmp_path,
        monkeypatch,
    ):
        """``package set <name> --ref <sha>`` stores SHA without network."""
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(
            marketplace,
            ["package", "set", "existing-package", "--ref", _FAKE_SHA],
        )
        assert result.exit_code == 0, result.output

    def test_set_ref_nonexistent_package_exits(
        self,
        runner,
        tmp_path,
        monkeypatch,
    ):
        """``package set <unknown> --ref HEAD`` errors on missing package."""
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(
            marketplace,
            ["package", "set", "nonexistent", "--ref", "HEAD"],
        )
        assert result.exit_code == 2
        assert "not found" in result.output
