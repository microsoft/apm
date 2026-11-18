"""Tests for the top-level ``apm view`` command (renamed from ``apm info``)."""

import contextlib
import json
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.models.dependency.types import GitReferenceType, RemoteRef


def test_view_json_installed_optional_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installed details expose null metadata and real counts, not display placeholders."""
    monkeypatch.chdir(tmp_path)
    package = tmp_path / "apm_modules" / "org" / "minimal"
    package.mkdir(parents=True)
    (package / "apm.yml").write_text("name: minimal\nversion: 1.0.0\n")
    instructions = package / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    (instructions / "rules.instructions.md").write_text("Follow the rules.\n")

    result = CliRunner().invoke(cli, ["view", "org/minimal", "--registry", "ignored", "-f", "json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "schema_version": 1,
        "data": {
            "name": "minimal",
            "version": "1.0.0",
            "description": None,
            "author": None,
            "source": None,
            "ref": None,
            "commit": None,
            "install_path": str(package),
            "context_files": {"instructions": 1, "agents": 0, "contexts": 0},
            "workflows": 0,
            "hooks": 0,
        },
    }
    assert "\x1b" not in result.stdout
    assert "--registry is only valid" in result.stderr


def test_view_json_marketplace_details() -> None:
    """Marketplace detail mode exposes its approved fields through the root CLI."""
    from apm_cli.marketplace.models import MarketplaceManifest, MarketplacePlugin

    plugin = MarketplacePlugin(
        name="review",
        source="org/review",
        version="2.0.0",
        description="Review changes",
        tags=("quality", "python"),
    )
    with (
        patch("apm_cli.marketplace.registry.get_marketplace_by_name"),
        patch(
            "apm_cli.marketplace.client.fetch_or_cache",
            return_value=MarketplaceManifest(name="tools", plugins=(plugin,)),
        ),
        patch(
            "apm_cli.marketplace.resolver.resolve_marketplace_plugin",
            return_value=("org/review#v2.0.0", None),
        ),
    ):
        text = CliRunner().invoke(cli, ["view", "review@tools"])
        explicit_text = CliRunner().invoke(cli, ["view", "review@tools", "-f", "text"])
        result = CliRunner().invoke(cli, ["view", "review@tools", "--format", "json"])

    assert text.exit_code == explicit_text.exit_code == 0
    assert text.stdout == explicit_text.stdout
    assert "Plugin: review" in text.stdout
    assert "apm install review@tools" in text.stdout
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "schema_version": 1,
        "data": {
            "name": "review",
            "marketplace": "tools",
            "version": "2.0.0",
            "description": "Review changes",
            "source": "org/review",
            "resolved": "org/review#v2.0.0",
            "tags": ["quality", "python"],
            "install": "review@tools",
        },
    }


def test_view_json_missing_marketplace_details_and_notices() -> None:
    """Unresolved optional values are null and owner notices stay off stdout."""
    import click

    from apm_cli.marketplace.models import MarketplaceManifest, MarketplacePlugin
    from apm_cli.utils.console import _rich_info

    def lookup(name: str) -> None:
        click.echo("Registry notice")

    def fetch(source: object) -> MarketplaceManifest:
        click.echo("Fetch notice")
        return MarketplaceManifest(name="tools", plugins=(MarketplacePlugin(name="review"),))

    with (
        patch(
            "apm_cli.cli._check_and_notify_updates", side_effect=lambda: _rich_info("Update notice")
        ),
        patch("apm_cli.marketplace.registry.get_marketplace_by_name", side_effect=lookup),
        patch("apm_cli.marketplace.client.fetch_or_cache", side_effect=fetch),
        patch(
            "apm_cli.marketplace.resolver.resolve_marketplace_plugin",
            side_effect=ValueError("not resolvable"),
        ),
    ):
        result = CliRunner().invoke(cli, ["view", "review@tools", "-fjson"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"] == {
        "name": "review",
        "marketplace": "tools",
        "version": None,
        "description": None,
        "source": None,
        "resolved": None,
        "tags": [],
        "install": "review@tools",
    }
    assert "Registry notice" in result.stderr
    assert "Fetch notice" in result.stderr
    assert "Update notice" in result.stderr
    assert "\x1b" not in result.stdout


@pytest.mark.parametrize(
    ("mode", "arguments", "code"),
    [
        ("absent", ["org/missing"], "modules_not_found"),
        ("empty", ["org/missing"], "package_not_found"),
        ("empty", ["../outside"], "package_not_found"),
        ("malformed", ["org/broken"], "package_read_failed"),
        ("empty", ["org/missing", "invalid"], "invalid_field"),
    ],
)
def test_view_json_installed_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    arguments: list[str],
    code: str,
) -> None:
    """Handled local failures are nonzero with a stable stderr error document."""
    monkeypatch.chdir(tmp_path)
    if mode != "absent":
        (tmp_path / "apm_modules").mkdir()
    if mode == "malformed":
        package = tmp_path / "apm_modules" / "org" / "broken"
        package.mkdir(parents=True)
        (package / "apm.yml").write_text("name: [unclosed\n")

    result = CliRunner().invoke(cli, ["view", *arguments, "--format=json"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr.splitlines()[-1]) == {"schema_version": 1, "error": code}


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        ("lookup", "marketplace_not_found"),
        ("fetch", "marketplace_fetch_failed"),
        ("plugin", "plugin_not_found"),
    ],
)
def test_view_json_marketplace_errors(failure: str, code: str) -> None:
    """Marketplace owner failures never leak raw exception text into the error code."""
    from apm_cli.marketplace.errors import MarketplaceFetchError, MarketplaceNotFoundError
    from apm_cli.marketplace.models import MarketplaceManifest

    with (
        patch(
            "apm_cli.marketplace.registry.get_marketplace_by_name",
            side_effect=MarketplaceNotFoundError("tools") if failure == "lookup" else None,
        ),
        patch(
            "apm_cli.marketplace.client.fetch_or_cache",
            return_value=MarketplaceManifest(name="tools"),
            side_effect=(
                MarketplaceFetchError("tools", "private-reason") if failure == "fetch" else None
            ),
        ),
    ):
        result = CliRunner().invoke(cli, ["view", "review@tools", "--format", "json"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr) == {"schema_version": 1, "error": code}
    assert "private-reason" not in result.stderr


@pytest.mark.parametrize(
    "arguments",
    [
        ["--file", "missing.md", "--format", "json"],
        ["org/repo", "versions", "-f", "json"],
        ["review@tools", "versions", "--format=json"],
        ["--format", "json"],
        ["org/repo", "--format", "yaml"],
        ["org/repo", "--format"],
    ],
)
def test_view_json_usage_boundary(arguments: list[str]) -> None:
    """Excluded detail modes and parser failures retain Click's usage-error boundary."""
    result = CliRunner().invoke(cli, ["view", *arguments])

    assert result.exit_code == 2
    assert result.stdout == ""
    if arguments[-1] != "--format":
        assert "Usage:" in result.stderr
    assert "Error:" in result.stderr
    assert "schema_version" not in result.stderr


@pytest.mark.parametrize("global_scope", [False, True])
def test_view_json_installed_scope_and_lockfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, global_scope: bool
) -> None:
    """Both scopes use the same installed location and lock metadata as text mode."""
    from apm_cli.deps.lockfile import LockedDependency, LockFile

    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setenv("APM_HOME", str(home))
    for root, name in ((project, "project-review"), (home, "global-review")):
        package = root / "apm_modules" / "org" / "review"
        package.mkdir(parents=True)
        (package / "apm.yml").write_text(
            f"name: {name}\nversion: 2.0.0\ndescription: Review changes\nauthor: Alice\n"
        )
        LockFile(
            dependencies={
                "org/review": LockedDependency(
                    repo_url="org/review",
                    resolved_ref=f"{name}-ref",
                    resolved_commit="aabbccddeeff0011223344556677889900112233",
                    source="registry",
                )
            }
        ).write(root / "apm.lock.yaml")

    args = ["view", "org/review", "--format", "json"]
    if global_scope:
        args.append("--global")
    result = CliRunner().invoke(cli, args)

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    expected_name = "global-review" if global_scope else "project-review"
    expected_root = home if global_scope else project
    assert data["name"] == expected_name
    assert data["ref"] == f"{expected_name}-ref"
    assert data["commit"] == "aabbccddeeff0011223344556677889900112233"
    assert data["source"] == "registry"
    assert data["description"] == "Review changes"
    assert data["author"] == "Alice"
    assert data["install_path"] == str(expected_root / "apm_modules" / "org" / "review")


@pytest.mark.parametrize("manifest", ["name: minimal\n", ""])
def test_view_json_missing_installed_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manifest: str
) -> None:
    """Incomplete manifests and skill-only installations use null missing metadata."""
    monkeypatch.chdir(tmp_path)
    package = tmp_path / "apm_modules" / "org" / "minimal"
    package.mkdir(parents=True)
    if manifest:
        (package / "apm.yml").write_text(manifest)
    else:
        (package / "SKILL.md").write_text("# Minimal\n")

    result = CliRunner().invoke(cli, ["view", "org/minimal", "-f", "json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert data["name"] == "minimal"
    for field in ("version", "description", "author", "source", "ref", "commit"):
        assert data[field] is None


@pytest.mark.parametrize("structured_source", [False, True])
def test_view_json_sanitizes_marketplace_strings(structured_source: bool) -> None:
    """URL-bearing metadata is allowlisted and sanitized, including embedded URLs."""
    from apm_cli.marketplace.models import MarketplaceManifest, MarketplacePlugin

    url = "https://alice:PASSWORD@example.com/org/review?token=SECRET#PRIVATE"
    source = {"type": "url", "url": url, "token": "EXTRA_SECRET"} if structured_source else url
    plugin = MarketplacePlugin(
        name="review",
        source=source,
        description=f"Read {url}",
        tags=(url,),
    )
    with (
        patch("apm_cli.marketplace.registry.get_marketplace_by_name"),
        patch(
            "apm_cli.marketplace.client.fetch_or_cache",
            return_value=MarketplaceManifest(name="tools", plugins=(plugin,)),
        ),
        patch(
            "apm_cli.marketplace.resolver.resolve_marketplace_plugin",
            return_value=(url, None),
        ),
    ):
        result = CliRunner().invoke(cli, ["view", "review@tools", "-f", "json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    for value in (
        data["source"].split()[-1],
        data["description"].split()[-1],
        data["resolved"],
        data["tags"][0],
    ):
        parsed = urlparse(value)
        assert parsed.hostname == "example.com"
        assert parsed.path == "/org/review"
        assert parsed.username is None
        assert parsed.password is None
        assert parsed.query == ""
        assert parsed.fragment == ""
    for secret in ("PASSWORD", "SECRET", "PRIVATE"):
        assert secret not in result.stdout


@pytest.mark.parametrize(
    "source",
    [
        {"type": ["raw"], "url": {"token": "RAW_SECRET"}, "ref": ["RAW_SECRET"]},
        "file://[",
    ],
)
def test_view_json_unusable_optional_source_is_null(source: object) -> None:
    """Malformed source fields are omitted safely rather than serialized as raw metadata."""
    from apm_cli.marketplace.models import MarketplaceManifest, MarketplacePlugin

    plugin = MarketplacePlugin(name="review", source=source)
    with (
        patch("apm_cli.marketplace.registry.get_marketplace_by_name"),
        patch(
            "apm_cli.marketplace.client.fetch_or_cache",
            return_value=MarketplaceManifest(name="tools", plugins=(plugin,)),
        ),
        patch(
            "apm_cli.marketplace.resolver.resolve_marketplace_plugin",
            side_effect=ValueError("not resolvable"),
        ),
    ):
        result = CliRunner().invoke(cli, ["view", "review@tools", "-f", "json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert data["source"] is None
    assert data["resolved"] is None
    assert "RAW_SECRET" not in result.stdout


def test_view_json_omits_cache_locations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Internal source and resolved cache paths never appear in detail output."""
    from apm_cli.deps.lockfile import LockedDependency, LockFile
    from apm_cli.marketplace.models import MarketplaceManifest, MarketplacePlugin

    monkeypatch.chdir(tmp_path)
    cache = tmp_path / "cache"
    monkeypatch.setenv("APM_CACHE_DIR", str(cache))
    cache_source = str(cache / "git" / "private-package")
    plugin = MarketplacePlugin(name="review", source=cache_source)
    with (
        patch("apm_cli.marketplace.registry.get_marketplace_by_name"),
        patch(
            "apm_cli.marketplace.client.fetch_or_cache",
            return_value=MarketplaceManifest(name="tools", plugins=(plugin,)),
        ),
        patch(
            "apm_cli.marketplace.resolver.resolve_marketplace_plugin",
            return_value=(cache_source, None),
        ),
    ):
        result = CliRunner().invoke(cli, ["view", "review@tools", "-f", "json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert data["source"] is None
    assert data["resolved"] is None
    assert cache_source not in result.stdout
    package = tmp_path / "apm_modules" / "org" / "review"
    package.mkdir(parents=True)
    (package / "apm.yml").write_text("name: review\nversion: 1.0.0\n")
    LockFile(
        dependencies={"org/review": LockedDependency(repo_url="org/review", source=cache_source)}
    ).write(tmp_path / "apm.lock.yaml")

    installed = CliRunner().invoke(cli, ["view", "org/review", "-f", "json"])

    assert installed.exit_code == 0, installed.output
    data = json.loads(installed.stdout)["data"]
    assert data["source"] is None
    assert data["install_path"] == str(package)
    assert cache_source not in installed.stdout


def test_view_json_sanitizes_installed_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installed details preserve literal metadata while scrubbing credential URLs."""
    from apm_cli.deps.lockfile import LockedDependency, LockFile

    monkeypatch.chdir(tmp_path)
    package = tmp_path / "apm_modules" / "org" / "review"
    package.mkdir(parents=True)
    url = "https://alice:PASSWORD@example.com/org/review?token=SECRET#PRIVATE"
    (package / "apm.yml").write_text(
        f'name: review\nversion: 1.0.0\ndescription: "Read {url}"\nauthor: unknown\n'
    )
    LockFile(
        dependencies={"org/review": LockedDependency(repo_url="org/review", source=url)}
    ).write(tmp_path / "apm.lock.yaml")
    (package / "run.prompt.md").write_text("Run review.\n")
    hooks = package / "hooks"
    hooks.mkdir()
    (hooks / "hooks.json").write_text("{}\n")
    default_text = CliRunner().invoke(cli, ["view", "org/review"])
    explicit_text = CliRunner().invoke(cli, ["view", "org/review", "--format", "text"])
    result = CliRunner().invoke(cli, ["view", "org/review", "-f", "json"])

    assert default_text.exit_code == explicit_text.exit_code == 0
    assert default_text.stdout == explicit_text.stdout
    assert "Package Info:" in default_text.stdout
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    for value in (data["source"], data["description"].split()[-1]):
        parsed = urlparse(value)
        assert parsed.hostname == "example.com"
        assert parsed.path == "/org/review"
        assert parsed.username is None
        assert parsed.query == ""
        assert parsed.fragment == ""
    assert data["author"] == "unknown"
    assert data["install_path"] == str(package)
    assert data["workflows"] == 1
    assert data["hooks"] == 1
    for secret in ("PASSWORD", "SECRET", "PRIVATE"):
        assert secret not in result.stdout


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

    def test_view_versions_routes_registry_dep_to_registry_api(self):
        """display_versions delegates to the registry API for a lockfile-confirmed registry dep."""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text("name: testproject\nversion: 1.0.0\n")
            with (
                patch(
                    "apm_cli.commands.view._lookup_lockfile_ref",
                    return_value=("^1.0.0", "", "registry"),
                ),
                patch("apm_cli.commands.view._display_registry_versions") as mock_reg,
                patch("apm_cli.commands.view.GitHubPackageDownloader") as mock_gh,
            ):
                result = self.runner.invoke(cli, ["view", "myorg/myrepo", "versions"])
        assert result.exit_code == 0
        mock_reg.assert_called_once()
        mock_gh.return_value.list_remote_refs.assert_not_called()

    def test_view_versions_routes_unlocked_shorthand_to_default_registry(self):
        """No lockfile signal, but a default registry is configured -> registry API.

        Mirrors how ``apm install`` routes an unscoped shorthand to the default
        registry. Without this the unlocked shorthand fell through to git.
        """
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text("name: testproject\nversion: 1.0.0\n")
            with (
                patch(
                    "apm_cli.commands.view._lookup_lockfile_ref",
                    return_value=("", "", ""),  # not in lockfile
                ),
                patch(
                    "apm_cli.commands.view._effective_default_registry",
                    return_value="jfrog-demo",
                ),
                patch("apm_cli.commands.view._display_registry_versions") as mock_reg,
                patch("apm_cli.commands.view.GitHubPackageDownloader") as mock_gh,
            ):
                result = self.runner.invoke(cli, ["view", "myorg/myrepo", "versions"])
        assert result.exit_code == 0
        mock_reg.assert_called_once()
        mock_gh.return_value.list_remote_refs.assert_not_called()

    def test_view_versions_unlocked_shorthand_no_default_uses_git(self):
        """No lockfile signal and no default registry -> git path (keeps git working)."""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text("name: testproject\nversion: 1.0.0\n")
            with (
                patch(
                    "apm_cli.commands.view._lookup_lockfile_ref",
                    return_value=("", "", ""),
                ),
                patch(
                    "apm_cli.commands.view._effective_default_registry",
                    return_value=None,
                ),
                patch("apm_cli.commands.view._display_registry_versions") as mock_reg,
                patch("apm_cli.commands.view.GitHubPackageDownloader") as mock_gh,
            ):
                mock_gh.return_value.list_remote_refs.return_value = []
                result = self.runner.invoke(cli, ["view", "myorg/myrepo", "versions"])
        assert result.exit_code == 0
        mock_reg.assert_not_called()
        mock_gh.return_value.list_remote_refs.assert_called_once()

    def test_view_versions_explicit_registry_flag_forces_registry(self):
        """--registry NAME forces the registry path and passes the name through."""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text("name: testproject\nversion: 1.0.0\n")
            with (
                patch("apm_cli.commands.view._display_registry_versions") as mock_reg,
                patch("apm_cli.commands.view.GitHubPackageDownloader") as mock_gh,
            ):
                result = self.runner.invoke(
                    cli, ["view", "myorg/myrepo", "versions", "--registry", "myreg"]
                )
        assert result.exit_code == 0
        mock_reg.assert_called_once()
        assert mock_reg.call_args.kwargs.get("registry_name") == "myreg"
        mock_gh.return_value.list_remote_refs.assert_not_called()

    def test_view_versions_bare_registry_flag_forces_registry(self):
        """Bare --registry (no NAME) parses as an empty value and forces the registry.

        Guards against Click option drift (e.g. --registry accidentally becoming
        value-required). registry_name is passed as None so the lockfile/default
        registry is used.
        """
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text("name: testproject\nversion: 1.0.0\n")
            with (
                patch("apm_cli.commands.view._display_registry_versions") as mock_reg,
                patch("apm_cli.commands.view.GitHubPackageDownloader") as mock_gh,
            ):
                result = self.runner.invoke(cli, ["view", "myorg/myrepo", "versions", "--registry"])
        assert result.exit_code == 0
        mock_reg.assert_called_once()
        # "" (bare flag) is normalized to None -> use lockfile/default registry.
        assert mock_reg.call_args.kwargs.get("registry_name") is None
        mock_gh.return_value.list_remote_refs.assert_not_called()

    def test_view_versions_scp_git_ref_forces_git_even_with_default_registry(self):
        """An SCP-style git ref (arbitrary user) routes to git, not the registry."""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text("name: testproject\nversion: 1.0.0\n")
            with (
                patch(
                    "apm_cli.commands.view._lookup_lockfile_ref",
                    return_value=("", "", ""),
                ),
                patch(
                    "apm_cli.commands.view._effective_default_registry",
                    return_value="jfrog-demo",
                ),
                patch("apm_cli.commands.view._display_registry_versions") as mock_reg,
                patch("apm_cli.commands.view.GitHubPackageDownloader") as mock_gh,
            ):
                mock_gh.return_value.list_remote_refs.return_value = []
                result = self.runner.invoke(
                    cli, ["view", "alice@github.com:myorg/myrepo", "versions"]
                )
        assert result.exit_code == 0
        mock_reg.assert_not_called()
        mock_gh.return_value.list_remote_refs.assert_called_once()

    def test_view_versions_unknown_registry_name_exits_with_error(self):
        """--registry UNKNOWN exits 1 and names the missing registry in the error message."""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text("name: testproject\nversion: 1.0.0\n")
            with patch(
                "apm_cli.deps.registry.config_loader.resolve_effective_registries",
                return_value=({"known-registry": "https://example.com/r"}, "known-registry"),
            ):
                result = self.runner.invoke(
                    cli, ["view", "myorg/myrepo", "versions", "--registry", "nonexistent"]
                )
        assert result.exit_code == 1
        assert "nonexistent" in result.output
        assert "not configured" in result.output

    def test_view_versions_explicit_git_url_forces_git_even_with_default_registry(self):
        """A full git URL routes to git even when a default registry is configured."""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text("name: testproject\nversion: 1.0.0\n")
            with (
                patch(
                    "apm_cli.commands.view._lookup_lockfile_ref",
                    return_value=("", "", ""),
                ),
                patch(
                    "apm_cli.commands.view._effective_default_registry",
                    return_value="jfrog-demo",
                ),
                patch("apm_cli.commands.view._display_registry_versions") as mock_reg,
                patch("apm_cli.commands.view.GitHubPackageDownloader") as mock_gh,
            ):
                mock_gh.return_value.list_remote_refs.return_value = []
                result = self.runner.invoke(
                    cli, ["view", "https://github.com/myorg/myrepo", "versions"]
                )
        assert result.exit_code == 0
        mock_reg.assert_not_called()
        mock_gh.return_value.list_remote_refs.assert_called_once()

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
                    return_value=("v2.0.0", "abcdef1234567890deadbeef", ""),
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


class TestEffectiveDefaultRegistry:
    """_effective_default_registry honors the registries feature gate."""

    def test_returns_none_when_registry_feature_disabled(self, tmp_path) -> None:
        """A config.json default must not route view->registry when registries are off.

        Mirrors install/registry_wiring.get_effective_default_registry, which
        short-circuits to None when is_package_registry_enabled() is False.
        """
        from apm_cli.commands.view import _effective_default_registry

        with (
            patch(
                "apm_cli.deps.registry.feature_gate.is_package_registry_enabled",
                return_value=False,
            ),
            patch(
                "apm_cli.deps.registry.config_loader.resolve_effective_registries",
                return_value=({"jfrog-demo": "https://example/r"}, "jfrog-demo"),
            ) as mock_resolve,
        ):
            result = _effective_default_registry(tmp_path)

        assert result is None
        # Gate short-circuits before any registry resolution happens.
        mock_resolve.assert_not_called()

    def test_returns_default_when_enabled(self, tmp_path) -> None:
        from apm_cli.commands.view import _effective_default_registry

        with (
            patch(
                "apm_cli.deps.registry.feature_gate.is_package_registry_enabled",
                return_value=True,
            ),
            patch(
                "apm_cli.deps.registry.config_loader.resolve_effective_registries",
                return_value=({"jfrog-demo": "https://example/r"}, "jfrog-demo"),
            ),
        ):
            result = _effective_default_registry(tmp_path)

        assert result == "jfrog-demo"


class TestDisplayRegistryVersions:
    """Direct tests for _display_registry_versions error and output paths."""

    runner = CliRunner()

    def test_unknown_registry_name_exits_with_error(self, tmp_path) -> None:
        """An explicit --registry NAME that does not exist in config exits 1."""
        from apm_cli.commands.view import _display_registry_versions
        from apm_cli.core.command_logger import CommandLogger
        from apm_cli.models.dependency.reference import DependencyReference

        dep_ref = DependencyReference.parse("acme/web-skills")
        logger = CommandLogger("test")

        with (
            patch(
                "apm_cli.deps.registry.config_loader.resolve_effective_registries",
                return_value=({"known-reg": "https://known.example"}, "known-reg"),
            ),
            pytest.raises(SystemExit, match=r"1"),
        ):
            _display_registry_versions(
                dep_ref.repo_url, dep_ref, logger, registry_name="no-such-reg"
            )

    def test_no_registry_configured_exits_with_error(self, tmp_path) -> None:
        """When no registry is configured at all, _display_registry_versions exits 1."""
        from apm_cli.commands.view import _display_registry_versions
        from apm_cli.core.command_logger import CommandLogger
        from apm_cli.models.dependency.reference import DependencyReference

        dep_ref = DependencyReference.parse("acme/web-skills")
        logger = CommandLogger("test")

        with (
            patch(
                "apm_cli.deps.registry.config_loader.resolve_effective_registries",
                return_value=(None, None),
            ),
            patch(
                "apm_cli.deps.lockfile.LockFile.read",
                return_value=None,
            ),
            pytest.raises(SystemExit, match=r"1"),
        ):
            _display_registry_versions("acme/web-skills", dep_ref, logger, registry_name=None)

    def test_registry_client_error_exits_with_error(self) -> None:
        """RegistryError from the client is caught and exits 1."""
        from apm_cli.commands.view import _display_registry_versions
        from apm_cli.core.command_logger import CommandLogger
        from apm_cli.deps.registry.client import RegistryError
        from apm_cli.models.dependency.reference import DependencyReference

        dep_ref = DependencyReference.parse("acme/web-skills")
        logger = CommandLogger("test")

        with (
            patch(
                "apm_cli.deps.registry.config_loader.resolve_effective_registries",
                return_value=({"my-reg": "https://reg.example"}, "my-reg"),
            ),
            patch(
                "apm_cli.deps.registry.auth.resolve_for_url",
                return_value=None,
            ),
            patch(
                "apm_cli.deps.registry.client.RegistryClient.list_versions",
                side_effect=RegistryError("connection refused"),
            ),
            pytest.raises(SystemExit, match=r"1"),
        ):
            _display_registry_versions("acme/web-skills", dep_ref, logger, registry_name=None)

    def test_happy_path_returns_versions(self) -> None:
        """Happy path: RegistryClient returns versions and the function returns normally."""
        from apm_cli.commands.view import _display_registry_versions
        from apm_cli.core.command_logger import CommandLogger
        from apm_cli.models.dependency.reference import DependencyReference

        dep_ref = DependencyReference.parse("acme/web-skills")
        logger = CommandLogger("test")

        version_entry = MagicMock()
        version_entry.version = "1.2.3"
        version_entry.published_at = "2025-01-01T00:00:00Z"

        with (
            patch(
                "apm_cli.deps.registry.config_loader.resolve_effective_registries",
                return_value=({"my-reg": "https://reg.example"}, "my-reg"),
            ),
            patch(
                "apm_cli.deps.registry.auth.resolve_for_url",
                return_value=None,
            ),
            patch(
                "apm_cli.deps.registry.client.RegistryClient.list_versions",
                return_value=[version_entry],
            ),
            patch("rich.console.Console") as mock_console_cls,
        ):
            mock_console = MagicMock()
            mock_console_cls.return_value = mock_console
            # Should not raise or exit
            _display_registry_versions("acme/web-skills", dep_ref, logger, registry_name=None)
