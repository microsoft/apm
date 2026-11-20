"""Tests for the top-level ``apm view`` command (renamed from ``apm info``) (pure logic cases)."""

import contextlib
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.models.dependency.types import GitReferenceType, RemoteRef

pytestmark = pytest.mark.unit

# ------------------------------------------------------------------
# Rich-fallback helper (same approach as test_deps_list_tree_info.py)
# ------------------------------------------------------------------


def _force_rich_fallback():
    """Context-manager that forces the text-only code path."""

    @contextlib.contextmanager
    def _ctx():
        keys = [
            "rich",
            "rich.console",
            "rich.table",
            "rich.tree",
            "rich.panel",
            "rich.text",
        ]
        originals = {k: sys.modules.get(k) for k in keys}

        for k in keys:
            stub = types.ModuleType(k)
            stub.__path__ = []

            def _raise(name, _k=k):
                raise ImportError(f"rich not available in test: {_k}")

            stub.__getattr__ = _raise
            sys.modules[k] = stub

        try:
            yield
        finally:
            for k, v in originals.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v

    return _ctx()


# ------------------------------------------------------------------
# Base class with temp-dir helpers
# ------------------------------------------------------------------


class _InfoCmdBase:
    """Shared CWD-management helpers."""

    def setup_method(self):
        self.runner = CliRunner()
        try:
            self.original_dir = os.getcwd()
        except FileNotFoundError:
            self.original_dir = str(Path(__file__).parent.parent.parent)
            os.chdir(self.original_dir)

    def teardown_method(self):
        try:
            os.chdir(self.original_dir)
        except (FileNotFoundError, OSError):
            repo_root = Path(__file__).parent.parent.parent
            os.chdir(str(repo_root))

    @contextlib.contextmanager
    def _chdir_tmp(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            try:
                os.chdir(tmp_dir)
                yield Path(tmp_dir)
            finally:
                os.chdir(self.original_dir)

    @staticmethod
    def _make_package(root: Path, org: str, repo: str, **kwargs) -> Path:
        pkg_dir = root / "apm_modules" / org / repo
        pkg_dir.mkdir(parents=True)
        version = kwargs.get("version", "1.0.0")
        description = kwargs.get("description", "A test package")
        author = kwargs.get("author", "TestAuthor")
        content = (
            f"name: {repo}\nversion: {version}\ndescription: {description}\nauthor: {author}\n"
        )
        (pkg_dir / "apm.yml").write_text(content)
        return pkg_dir


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------


class TestViewCommand(_InfoCmdBase):
    """Tests for the top-level ``apm view`` command."""

    # -- basic metadata display -------------------------------------------

    # -- missing apm_modules/ ---------------------------------------------

    def test_view_no_apm_modules(self):
        """``apm view`` exits with error when apm_modules/ is missing."""
        with self._chdir_tmp():
            result = self.runner.invoke(cli, ["view", "noorg/norepo"])
        assert result.exit_code == 1

    # -- field: versions (placeholder) ------------------------------------

    def test_view_versions_lists_refs(self):
        """``apm view org/repo versions`` shows tags and branches."""
        mock_refs = [
            RemoteRef(name="v2.0.0", ref_type=GitReferenceType.TAG, commit_sha="aabbccdd11223344"),
            RemoteRef(name="v1.0.0", ref_type=GitReferenceType.TAG, commit_sha="11223344aabbccdd"),
            RemoteRef(name="main", ref_type=GitReferenceType.BRANCH, commit_sha="deadbeef12345678"),
        ]
        with (
            patch("apm_cli.commands.view.GitHubPackageDownloader") as mock_cls,
            patch("apm_cli.commands.view.AuthResolver"),
        ):
            mock_cls.return_value.list_remote_refs.return_value = mock_refs
            with _force_rich_fallback():
                result = self.runner.invoke(cli, ["view", "myorg/myrepo", "versions"])
        assert result.exit_code == 0
        assert "v2.0.0" in result.output
        assert "v1.0.0" in result.output
        assert "main" in result.output
        assert "tag" in result.output
        assert "branch" in result.output
        assert "aabbccdd" in result.output
        assert "deadbeef" in result.output

    def test_view_versions_renders_ascii_table(self):
        """The Rich versions table draws its box in ASCII (encoding rule, #3994)."""
        mock_refs = [
            RemoteRef(name="v1.0.0", ref_type=GitReferenceType.TAG, commit_sha="11223344aabbccdd"),
        ]
        with (
            patch("apm_cli.commands.view.GitHubPackageDownloader") as mock_cls,
            patch("apm_cli.commands.view.AuthResolver"),
        ):
            mock_cls.return_value.list_remote_refs.return_value = mock_refs
            result = self.runner.invoke(cli, ["view", "myorg/myrepo", "versions"])
        assert result.exit_code == 0
        assert "v1.0.0" in result.output
        assert "+--" in result.output
        assert result.output.isascii(), result.output

    def test_view_versions_empty_refs(self):
        """``apm view org/repo versions`` with no refs shows info message."""
        with (
            patch("apm_cli.commands.view.GitHubPackageDownloader") as mock_cls,
            patch("apm_cli.commands.view.AuthResolver"),
        ):
            mock_cls.return_value.list_remote_refs.return_value = []
            result = self.runner.invoke(cli, ["view", "myorg/myrepo", "versions"])
        assert result.exit_code == 0
        assert "no versions found" in result.output.lower()

    def test_view_versions_runtime_error(self):
        """``apm view org/repo versions`` exits 1 on RuntimeError."""
        with (
            patch("apm_cli.commands.view.GitHubPackageDownloader") as mock_cls,
            patch("apm_cli.commands.view.AuthResolver"),
        ):
            mock_cls.return_value.list_remote_refs.side_effect = RuntimeError("auth failed")
            result = self.runner.invoke(cli, ["view", "myorg/myrepo", "versions"])
        assert result.exit_code == 1
        assert "failed to list versions" in result.output.lower()

    def test_view_versions_with_ref_shorthand(self):
        """``apm view owner/repo#v1.0 versions`` parses ref correctly."""
        mock_refs = [
            RemoteRef(name="v1.0.0", ref_type=GitReferenceType.TAG, commit_sha="abcdef1234567890"),
        ]
        with (
            patch("apm_cli.commands.view.GitHubPackageDownloader") as mock_cls,
            patch("apm_cli.commands.view.AuthResolver"),
        ):
            mock_cls.return_value.list_remote_refs.return_value = mock_refs
            with _force_rich_fallback():
                result = self.runner.invoke(cli, ["view", "myorg/myrepo#v1.0", "versions"])
        assert result.exit_code == 0
        assert "v1.0.0" in result.output

    def test_view_versions_does_not_require_apm_modules(self):
        """``apm view org/repo versions`` works without apm_modules/."""
        mock_refs = [
            RemoteRef(name="main", ref_type=GitReferenceType.BRANCH, commit_sha="1234567890abcdef"),
        ]
        with self._chdir_tmp():
            # No apm_modules/ created -- should still succeed
            with (
                patch("apm_cli.commands.view.GitHubPackageDownloader") as mock_cls,
                patch("apm_cli.commands.view.AuthResolver"),
            ):
                mock_cls.return_value.list_remote_refs.return_value = mock_refs
                with _force_rich_fallback():
                    result = self.runner.invoke(cli, ["view", "myorg/myrepo", "versions"])
        assert result.exit_code == 0
        assert "main" in result.output

    # -- invalid field ----------------------------------------------------

    # -- short name resolution --------------------------------------------

    # -- package not found ------------------------------------------------

    # -- SKILL.md-only package (no apm.yml) --------------------------------

    # -- bare package (no context files / no workflows) -------------------

    # -- no args: Click should show error / usage -------------------------

    def test_view_no_args_shows_error(self):
        """``apm view`` with no arguments shows an error (PACKAGE is required)."""
        result = self.runner.invoke(cli, ["view"])
        # Click exits 2 for missing required arguments
        assert result.exit_code == 2
        # Should mention the missing argument or show usage
        assert (
            "PACKAGE" in result.output
            or "Missing argument" in result.output
            or "Usage" in result.output
        )

    # -- DependencyReference.parse failure for versions field -------------

    def test_view_versions_invalid_parse(self):
        """``apm view <pkg> versions`` exits 1 when DependencyReference.parse raises ValueError."""
        with patch("apm_cli.commands.view.DependencyReference") as mock_dep_ref_cls:
            mock_dep_ref_cls.parse.side_effect = ValueError("unsupported host: ftp")
            result = self.runner.invoke(cli, ["view", "ftp://bad-host/invalid", "versions"])
        assert result.exit_code == 1
        assert "invalid" in result.output.lower() or "ftp" in result.output.lower()

    # -- path traversal prevention ----------------------------------------

    # -- global flag -------------------------------------------------------

    # -- display with context files, workflows, hooks ----------------------

    # -- lockfile ref/commit display ---------------------------------------


# ------------------------------------------------------------------
# _lookup_lockfile_ref unit tests
# ------------------------------------------------------------------


class TestLookupLockfileRef(_InfoCmdBase):
    """Direct tests for the _lookup_lockfile_ref helper."""

    def test_returns_empty_when_no_lockfile(self):
        """Returns ('', '') when lockfile does not exist."""
        from apm_cli.commands.view import _lookup_lockfile_ref

        with self._chdir_tmp() as tmp:
            ref, commit, _source = _lookup_lockfile_ref("org/repo", tmp)
        assert ref == ""
        assert commit == ""
        assert _source == ""

    def test_exact_match(self):
        """Returns ref/commit for exact lockfile key match."""
        from apm_cli.commands.view import _lookup_lockfile_ref

        mock_dep = MagicMock()
        mock_dep.resolved_ref = "v1.0.0"
        mock_dep.resolved_commit = "abc123"
        mock_lockfile = MagicMock()
        mock_lockfile.dependencies = {"org/repo": mock_dep}

        with (
            patch("apm_cli.deps.lockfile.LockFile") as mock_lf,
            patch("apm_cli.deps.lockfile.get_lockfile_path"),
            patch("apm_cli.deps.lockfile.migrate_lockfile_if_needed"),
        ):  # patched to prevent real I/O
            mock_lf.read.return_value = mock_lockfile
            ref, commit, _source = _lookup_lockfile_ref("org/repo", Path("/fake"))
        assert ref == "v1.0.0"
        assert commit == "abc123"

    def test_substring_match(self):
        """Falls back to substring match when exact key misses."""
        from apm_cli.commands.view import _lookup_lockfile_ref

        mock_dep = MagicMock()
        mock_dep.resolved_ref = "main"
        mock_dep.resolved_commit = "deadbeef"
        mock_lockfile = MagicMock()
        # Key includes "org/repo" as substring but exact .get("org/repo") misses
        mock_lockfile.dependencies = {
            "github.com/org/repo": mock_dep,
        }

        with (
            patch("apm_cli.deps.lockfile.LockFile") as mock_lf,
            patch("apm_cli.deps.lockfile.get_lockfile_path"),
            patch("apm_cli.deps.lockfile.migrate_lockfile_if_needed"),
        ):  # patched to prevent real I/O
            mock_lf.read.return_value = mock_lockfile
            ref, commit, _source = _lookup_lockfile_ref("org/repo", Path("/fake"))
        assert ref == "main"
        assert commit == "deadbeef"

    def test_no_matching_dep(self):
        """Returns ('', '') when no dependency matches."""
        from apm_cli.commands.view import _lookup_lockfile_ref

        mock_lockfile = MagicMock()
        mock_lockfile.dependencies = {"other/pkg": MagicMock()}

        with (
            patch("apm_cli.deps.lockfile.LockFile") as mock_lf,
            patch("apm_cli.deps.lockfile.get_lockfile_path"),
            patch("apm_cli.deps.lockfile.migrate_lockfile_if_needed"),
        ):  # patched to prevent real I/O
            mock_lf.read.return_value = mock_lockfile
            ref, commit, _source = _lookup_lockfile_ref("nomatch", Path("/fake"))
        assert ref == ""
        assert commit == ""

    def test_exception_returns_empty(self):
        """Any exception is swallowed and empty strings returned."""
        from apm_cli.commands.view import _lookup_lockfile_ref

        with patch(
            "apm_cli.deps.lockfile.migrate_lockfile_if_needed",
            side_effect=RuntimeError("boom"),
        ):
            ref, commit, _source = _lookup_lockfile_ref("x", Path("/fake"))
        assert ref == ""
        assert commit == ""

    def test_lockfile_read_returns_none(self):
        """Returns ('', '') when LockFile.read returns None."""
        from apm_cli.commands.view import _lookup_lockfile_ref

        with (
            patch("apm_cli.deps.lockfile.LockFile") as mock_lf,
            patch("apm_cli.deps.lockfile.get_lockfile_path"),
            patch("apm_cli.deps.lockfile.migrate_lockfile_if_needed"),
        ):  # patched to prevent real I/O
            mock_lf.read.return_value = None
            ref, commit, _source = _lookup_lockfile_ref("org/repo", Path("/fake"))
        assert ref == ""
        assert commit == ""


class TestInfoAlias(_InfoCmdBase):
    """Verify ``apm info`` still works as a hidden backward-compatible alias."""

    def test_info_alias_hidden_from_help(self):
        """``apm info`` does NOT appear in top-level ``--help`` output."""
        result = self.runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        # "view" should be visible; "info" should not
        assert "view" in result.output
        # Check that "info" doesn't appear as a listed command
        # (it may appear in other text, so check the commands section)
        lines = result.output.splitlines()
        command_lines = [  # noqa: F841
            l.strip()
            for l in lines  # noqa: E741
            if l.strip().startswith("info") and not l.strip().startswith("info")  # skip false match
        ]
        # More robust: "info" should not be in the commands listing
        # The help output lists commands like "  view    View package..."
        # "info" as hidden should be absent from this listing
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("info "):
                pytest.fail(f"'info' should be hidden but found in help: {line}")


# ------------------------------------------------------------------
# B4: ``apm view plugin@marketplace`` without ``versions`` field
# ------------------------------------------------------------------


class TestViewMarketplaceNoField(_InfoCmdBase):
    """``apm view plugin@marketplace`` (no field) shows marketplace plugin."""

    def test_marketplace_ref_shows_plugin(self):
        """``apm view plugin@mkt`` routes to _display_marketplace_plugin."""
        from apm_cli.marketplace.models import (
            MarketplaceManifest,
            MarketplacePlugin,
            MarketplaceSource,
        )

        plugin = MarketplacePlugin(
            name="my-plugin",
            source={"type": "github", "repo": "acme/plugin", "ref": "main"},
            version="2.0.0",
        )
        manifest = MarketplaceManifest(name="acme-tools", plugins=(plugin,))
        source = MarketplaceSource(name="acme-tools", owner="acme", repo="marketplace")

        with (
            patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                return_value=source,
            ),
            patch(
                "apm_cli.marketplace.client.fetch_or_cache",
                return_value=manifest,
            ),
            _force_rich_fallback(),
        ):
            result = self.runner.invoke(cli, ["view", "my-plugin@acme-tools"])

        assert result.exit_code == 0
        assert "my-plugin" in result.output
        assert "acme-tools" in result.output

    def test_marketplace_ref_hint_names_a_refused_entry_as_refused(self):
        """View never suggests installing an entry install refuses (#1326; A6, N3)."""
        from types import SimpleNamespace

        from apm_cli.marketplace.models import (
            MarketplaceManifest,
            MarketplacePlugin,
            MarketplaceSource,
        )

        plugin = MarketplacePlugin(
            name="x", source={"type": "github", "repo": "acme/tools"}, version="1.0.0"
        )
        manifest = MarketplaceManifest(name="corp-mkt", plugins=(plugin,))
        source = MarketplaceSource(name="corp-mkt", owner="corp", repo="marketplace")
        risky = SimpleNamespace(
            canonical="acme/tools", plugin=plugin, cross_repo_misconfig_risk=object()
        )

        def _refuse(resolution):
            from apm_cli.marketplace.errors import DependencyConfusionError

            raise DependencyConfusionError.__new__(DependencyConfusionError)

        with (
            patch("apm_cli.marketplace.registry.get_marketplace_by_name", return_value=source),
            patch("apm_cli.marketplace.client.fetch_or_cache", return_value=manifest),
            patch(
                "apm_cli.marketplace.resolver.resolve_marketplace_plugin", return_value=risky
            ) as resolve,
            patch(
                "apm_cli.marketplace.dependency_confusion.admit_marketplace_resolution",
                side_effect=_refuse,
            ),
            _force_rich_fallback(),
        ):
            result = self.runner.invoke(cli, ["view", "x@corp-mkt"])

        assert result.exit_code == 0, result.output
        resolve.assert_called_once_with("x", "corp-mkt")
        assert "apm install x@corp-mkt" not in result.output
        assert "Install: refused" in result.output

    def test_marketplace_ref_does_not_require_apm_modules(self):
        """``apm view plugin@mkt`` works without apm_modules/ directory."""
        from apm_cli.marketplace.models import (
            MarketplaceManifest,
            MarketplacePlugin,
            MarketplaceSource,
        )

        plugin = MarketplacePlugin(
            name="my-plugin",
            source={"type": "github", "repo": "acme/plugin", "ref": "main"},
            version="1.0.0",
        )
        manifest = MarketplaceManifest(name="acme-tools", plugins=(plugin,))
        source = MarketplaceSource(name="acme-tools", owner="acme", repo="marketplace")

        with self._chdir_tmp():
            # No apm_modules/ -- should still succeed
            with (
                patch(
                    "apm_cli.marketplace.registry.get_marketplace_by_name",
                    return_value=source,
                ),
                patch(
                    "apm_cli.marketplace.client.fetch_or_cache",
                    return_value=manifest,
                ),
            ):
                with _force_rich_fallback():
                    result = self.runner.invoke(cli, ["view", "my-plugin@acme-tools"])

        assert result.exit_code == 0

    def test_marketplace_ref_with_version_fragment(self):
        """``apm view plugin@mkt#ref`` (no field) shows plugin info."""
        from apm_cli.marketplace.models import (
            MarketplaceManifest,
            MarketplacePlugin,
            MarketplaceSource,
        )

        plugin = MarketplacePlugin(
            name="my-plugin",
            source={"type": "github", "repo": "acme/plugin", "ref": "v1.0.0"},
            version="1.0.0",
        )
        manifest = MarketplaceManifest(name="acme-tools", plugins=(plugin,))
        source = MarketplaceSource(name="acme-tools", owner="acme", repo="marketplace")

        with (
            patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                return_value=source,
            ),
            patch(
                "apm_cli.marketplace.client.fetch_or_cache",
                return_value=manifest,
            ),
            _force_rich_fallback(),
        ):
            result = self.runner.invoke(cli, ["view", "my-plugin@acme-tools"])

        assert result.exit_code == 0
        assert "1.0.0" in result.output
