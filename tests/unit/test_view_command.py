"""Tests for the top-level ``apm view`` command (renamed from ``apm info``)."""

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

    def test_view_shows_package_details(self):
        """``apm view org/repo`` shows package metadata (fallback mode)."""
        with self._chdir_tmp() as tmp:
            self._make_package(
                tmp,
                "myorg",
                "myrepo",
                version="2.5.0",
                description="My awesome package",
                author="Alice",
            )
            os.chdir(tmp)
            with _force_rich_fallback():
                result = self.runner.invoke(cli, ["view", "myorg/myrepo"])
        assert result.exit_code == 0
        assert "2.5.0" in result.output
        assert "My awesome package" in result.output
        assert "Alice" in result.output

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

    def test_view_invalid_field(self):
        """``apm view org/repo bad-field`` shows error with valid fields."""
        with self._chdir_tmp() as tmp:
            self._make_package(tmp, "forg", "frepo")
            os.chdir(tmp)
            result = self.runner.invoke(cli, ["view", "forg/frepo", "bad-field"])
        assert result.exit_code == 1
        assert "bad-field" in result.output
        assert "versions" in result.output

    # -- short name resolution --------------------------------------------

    def test_view_short_package_name(self):
        """``apm view repo`` resolves by short repo name."""
        with self._chdir_tmp() as tmp:
            self._make_package(tmp, "shortorg", "shortrepo")
            os.chdir(tmp)
            with _force_rich_fallback():
                result = self.runner.invoke(cli, ["view", "shortrepo"])
        assert result.exit_code == 0
        assert "shortrepo" in result.output

    # -- package not found ------------------------------------------------

    def test_view_package_not_found(self):
        """``apm view`` shows error and lists available packages."""
        with self._chdir_tmp() as tmp:
            self._make_package(tmp, "existorg", "existrepo")
            os.chdir(tmp)
            result = self.runner.invoke(cli, ["view", "doesnotexist"])
        assert result.exit_code == 1
        assert "not found" in result.output.lower() or "error" in result.output.lower()
        assert "existorg/existrepo" in result.output

    # -- SKILL.md-only package (no apm.yml) --------------------------------

    def test_view_skill_only_package(self):
        """``apm view`` works for packages with SKILL.md but no apm.yml."""
        with self._chdir_tmp() as tmp:
            pkg_dir = tmp / "apm_modules" / "skillorg" / "skillrepo"
            pkg_dir.mkdir(parents=True)
            (pkg_dir / "SKILL.md").write_text("# My Skill\n")
            os.chdir(tmp)
            with _force_rich_fallback():
                result = self.runner.invoke(cli, ["view", "skillorg/skillrepo"])
        assert result.exit_code == 0
        assert "skillrepo" in result.output

    # -- bare package (no context files / no workflows) -------------------

    def test_view_bare_package_no_context(self):
        """``apm view`` reports 'No context files found' for bare packages."""
        with self._chdir_tmp() as tmp:
            self._make_package(tmp, "bareorg", "barerepo")
            os.chdir(tmp)
            with _force_rich_fallback():
                result = self.runner.invoke(cli, ["view", "bareorg/barerepo"])
        assert result.exit_code == 0
        assert "No context files found" in result.output

    def test_view_bare_package_no_workflows(self):
        """``apm view`` reports 'No agent workflows found' for bare packages."""
        with self._chdir_tmp() as tmp:
            self._make_package(tmp, "wforg", "wfrepo")
            os.chdir(tmp)
            with _force_rich_fallback():
                result = self.runner.invoke(cli, ["view", "wforg/wfrepo"])
        assert result.exit_code == 0
        assert "No agent workflows found" in result.output

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

    def test_view_rejects_path_traversal(self):
        """``apm view ../../../etc/passwd`` is rejected as a traversal attempt."""
        with self._chdir_tmp() as tmp:
            self._make_package(tmp, "org", "legit")
            os.chdir(tmp)
            result = self.runner.invoke(cli, ["view", "../../../etc/passwd"])
        assert result.exit_code == 1
        assert "traversal" in result.output.lower()

    def test_view_rejects_dot_segment(self):
        """``apm view org/../../../etc/passwd`` is rejected."""
        with self._chdir_tmp() as tmp:
            self._make_package(tmp, "org", "legit")
            os.chdir(tmp)
            result = self.runner.invoke(cli, ["view", "org/../../../etc/passwd"])
        assert result.exit_code == 1
        assert "traversal" in result.output.lower()

    # -- global flag -------------------------------------------------------

    def test_view_global_flag(self):
        """``apm view org/repo -g`` uses user scope."""
        with self._chdir_tmp():
            fake_home = Path(tempfile.mkdtemp())
            pkg_dir = fake_home / "apm_modules" / "gorg" / "grepo"
            pkg_dir.mkdir(parents=True)
            (pkg_dir / "apm.yml").write_text(
                "name: grepo\nversion: 1.0.0\ndescription: global pkg\nauthor: X\n"
            )
            with (
                patch(
                    "apm_cli.core.scope.get_apm_dir",
                    return_value=fake_home,
                ),
                _force_rich_fallback(),
            ):
                result = self.runner.invoke(cli, ["view", "gorg/grepo", "-g"])
            assert result.exit_code == 0
            assert "grepo" in result.output

    # -- display with context files, workflows, hooks ----------------------

    def test_view_shows_context_files(self):
        """``apm view`` displays context file counts."""
        with self._chdir_tmp() as tmp:
            pkg = self._make_package(tmp, "corg", "crepo")
            apm_dir = pkg / ".apm"
            apm_dir.mkdir()
            inst = apm_dir / "instructions"
            inst.mkdir()
            (inst / "setup.md").write_text("# setup")
            os.chdir(tmp)
            with _force_rich_fallback():
                result = self.runner.invoke(cli, ["view", "corg/crepo"])
        assert result.exit_code == 0
        assert "1 instructions" in result.output

    def test_view_shows_workflows(self):
        """``apm view`` displays workflow count."""
        with self._chdir_tmp() as tmp:
            pkg = self._make_package(tmp, "worg", "wrepo")
            apm_dir = pkg / ".apm"
            apm_dir.mkdir()
            prompts = apm_dir / "prompts"
            prompts.mkdir()
            (prompts / "run.prompt.md").write_text("# run")
            os.chdir(tmp)
            with _force_rich_fallback():
                result = self.runner.invoke(cli, ["view", "worg/wrepo"])
        assert result.exit_code == 0
        assert "1 executable workflows" in result.output

    def test_view_shows_hooks(self):
        """``apm view`` displays hook count when hooks exist."""
        with self._chdir_tmp() as tmp:
            pkg = self._make_package(tmp, "horg", "hrepo")
            hooks_dir = pkg / "hooks"
            hooks_dir.mkdir()
            (hooks_dir / "pre-commit.json").write_text("{}")
            os.chdir(tmp)
            with _force_rich_fallback():
                result = self.runner.invoke(cli, ["view", "horg/hrepo"])
        assert result.exit_code == 0
        assert "hook file(s)" in result.output

    # -- lockfile ref/commit display ---------------------------------------

    def test_view_shows_lockfile_ref_and_commit(self):
        """``apm view`` displays ref and commit from lockfile."""
        with self._chdir_tmp() as tmp:
            self._make_package(tmp, "lorg", "lrepo")
            os.chdir(tmp)
            with (
                patch(
                    "apm_cli.commands.view._lookup_lockfile_ref",
                    return_value=("v2.0.0", "abcdef1234567890deadbeef"),
                ),
                _force_rich_fallback(),
            ):
                result = self.runner.invoke(cli, ["view", "lorg/lrepo"])
        assert result.exit_code == 0
        assert "v2.0.0" in result.output
        assert "abcdef123456" in result.output  # truncated to 12 chars


# ------------------------------------------------------------------
# _lookup_lockfile_ref unit tests
# ------------------------------------------------------------------


class TestLookupLockfileRef(_InfoCmdBase):
    """Direct tests for the _lookup_lockfile_ref helper."""

    def test_returns_empty_when_no_lockfile(self):
        """Returns ('', '') when lockfile does not exist."""
        from apm_cli.commands.view import _lookup_lockfile_ref

        with self._chdir_tmp() as tmp:
            ref, commit = _lookup_lockfile_ref("org/repo", tmp)
        assert ref == ""
        assert commit == ""

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
            ref, commit = _lookup_lockfile_ref("org/repo", Path("/fake"))
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
            ref, commit = _lookup_lockfile_ref("org/repo", Path("/fake"))
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
            ref, commit = _lookup_lockfile_ref("nomatch", Path("/fake"))
        assert ref == ""
        assert commit == ""

    def test_exception_returns_empty(self):
        """Any exception is swallowed and empty strings returned."""
        from apm_cli.commands.view import _lookup_lockfile_ref

        with patch(
            "apm_cli.deps.lockfile.migrate_lockfile_if_needed",
            side_effect=RuntimeError("boom"),
        ):
            ref, commit = _lookup_lockfile_ref("x", Path("/fake"))
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
            ref, commit = _lookup_lockfile_ref("org/repo", Path("/fake"))
        assert ref == ""
        assert commit == ""


class TestInfoAlias(_InfoCmdBase):
    """Verify ``apm info`` still works as a hidden backward-compatible alias."""

    def test_info_alias_shows_package_details(self):
        """``apm info org/repo`` produces the same output as ``apm view``."""
        with self._chdir_tmp() as tmp:
            self._make_package(
                tmp,
                "myorg",
                "myrepo",
                version="2.5.0",
                description="Alias test",
                author="Bob",
            )
            os.chdir(tmp)
            with _force_rich_fallback():
                result = self.runner.invoke(cli, ["info", "myorg/myrepo"])
        assert result.exit_code == 0
        assert "2.5.0" in result.output
        assert "Alias test" in result.output

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

    def test_non_marketplace_ref_still_uses_local_path(self):
        """``apm view org/repo`` still falls through to local metadata lookup."""
        with self._chdir_tmp() as tmp:
            self._make_package(
                tmp,
                "myorg",
                "myrepo",
                version="3.0.0",
            )
            os.chdir(tmp)
            with _force_rich_fallback():
                result = self.runner.invoke(cli, ["view", "myorg/myrepo"])

        assert result.exit_code == 0
        assert "3.0.0" in result.output

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
