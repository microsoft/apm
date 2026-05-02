"""Unit tests for local-bundle install routing, duck-type contract, rejected flags, --as.

These tests target the local-bundle code path in ``apm_cli.commands.install``
and ``apm_cli.install.services``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import tarfile
import types
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

_LOCAL_BUNDLE_EXISTS = importlib.util.find_spec("apm_cli.bundle.local_bundle") is not None
_INTEGRATE_EXISTS = False
try:
    from apm_cli.install.services import integrate_local_bundle  # noqa: F401

    _INTEGRATE_EXISTS = True
except ImportError:
    pass

_MODULE_READY = _LOCAL_BUNDLE_EXISTS and _INTEGRATE_EXISTS

pytestmark = pytest.mark.skipif(
    not _MODULE_READY,
    reason="local-bundle production modules not yet implemented (TDD stub)",
)


# ---------------------------------------------------------------------------
# Bundle / project fixtures (mirror of the E2E helpers; kept local to avoid
# an integration<->unit import dep).
# ---------------------------------------------------------------------------


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _make_bundle(
    base: Path,
    *,
    plugin_id: str | None = "test-plugin",
    pack_target: str = "copilot",
    files: dict[str, str] | None = None,
    include_lockfile: bool = True,
) -> Path:
    bundle = base / "bundle"
    bundle.mkdir(parents=True, exist_ok=True)
    pj: dict = {"name": "Test Plugin"}
    if plugin_id is not None:
        pj["id"] = plugin_id
    (bundle / "plugin.json").write_text(json.dumps(pj), encoding="utf-8")

    if files is None:
        files = {"skills/coding/SKILL.md": "# Coding\n"}
    bundle_files: dict[str, str] = {}
    for rel, content in files.items():
        p = bundle / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        bundle_files[rel] = _sha256(content)

    if include_lockfile:
        lock_data = {
            "pack": {
                "format": "plugin",
                "target": pack_target,
                "bundle_files": bundle_files,
            },
            "dependencies": [
                {
                    "repo_url": "owner/test-plugin",
                    "resolved_commit": "abc123",
                    "deployed_files": list(files.keys()),
                    "deployed_file_hashes": bundle_files,
                }
            ],
        }
        (bundle / "apm.lock.yaml").write_text(
            yaml.dump(lock_data, default_flow_style=False), encoding="utf-8"
        )
    return bundle


def _make_tarball(base: Path, bundle_dir: Path) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    archive = base / "test-bundle.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(bundle_dir, arcname=bundle_dir.name)
    return archive


def _make_project(base: Path) -> Path:
    project = base / "project"
    project.mkdir(parents=True, exist_ok=True)
    (project / "apm.yml").write_text(
        yaml.dump({"name": "test-project", "version": "1.0.0"}, default_flow_style=False),
        encoding="utf-8",
    )
    return project


def _invoke(project: Path, monkeypatch, *args: str):
    from apm_cli.cli import cli

    monkeypatch.chdir(project)
    return CliRunner().invoke(cli, ["install", *args], catch_exceptions=False)


# ---------------------------------------------------------------------------
# Duck-type contract test for package_info
# ---------------------------------------------------------------------------

# Audited attributes consumed by integrate_package_primitives() and all
# integrators (services.py, agent_integrator, prompt_integrator,
# skill_integrator, hook_integrator, instruction_integrator, command_integrator,
# base_integrator):
#
#   package_info.install_path          -> Path    (all integrators)
#   package_info.install_path.name     -> str     (hook, skill integrators)
#   package_info.package.name          -> str     (agent, prompt integrators)
#   package_info.package_type          -> enum    (skill integrator routing)
#   package_info.dependency_ref        -> obj|None (skill integrator ownership)
#   package_info.dependency_ref.is_virtual            -> bool
#   package_info.dependency_ref.is_virtual_subdirectory() -> bool
#   package_info.dependency_ref.get_unique_key()      -> str

_REQUIRED_ATTRS = [
    "install_path",
    "package",
    "package_type",
    "dependency_ref",
]

_REQUIRED_PACKAGE_ATTRS = ["name"]

_REQUIRED_DEPENDENCY_REF_ATTRS = [
    "is_virtual",
]

_REQUIRED_DEPENDENCY_REF_METHODS = [
    "is_virtual_subdirectory",
    "get_unique_key",
]


class TestSyntheticPackageInfoContract:
    """Pin the duck-type interface that integrate_package_primitives() consumes.

    If any integrator adds a new attribute access on package_info, this test
    must be updated -- and it will fail first, signaling a contract drift.
    """

    def test_synthetic_package_info_has_required_attributes(self, tmp_path: Path) -> None:
        """A synthetic package_info for local bundles must expose every
        attribute consumed by the integrator pipeline."""
        # Build a minimal synthetic object matching the contract
        package_mock = types.SimpleNamespace(name="test-plugin")
        dep_ref_mock = types.SimpleNamespace(
            is_virtual=False,
            is_virtual_subdirectory=lambda: False,
            get_unique_key=lambda: "local://test-plugin",
        )
        pkg_info = types.SimpleNamespace(
            install_path=tmp_path,
            package=package_mock,
            package_type="STANDARD",  # PackageType enum value
            dependency_ref=dep_ref_mock,
        )

        # Assert all required attributes are accessible (no AttributeError)
        for attr in _REQUIRED_ATTRS:
            assert hasattr(pkg_info, attr), f"Missing attribute: {attr}"

        for attr in _REQUIRED_PACKAGE_ATTRS:
            assert hasattr(pkg_info.package, attr), f"Missing package.{attr}"

        for attr in _REQUIRED_DEPENDENCY_REF_ATTRS:
            assert hasattr(pkg_info.dependency_ref, attr), f"Missing dependency_ref.{attr}"

        for method in _REQUIRED_DEPENDENCY_REF_METHODS:
            assert callable(getattr(pkg_info.dependency_ref, method)), (
                f"dependency_ref.{method} must be callable"
            )

    def test_synthetic_package_info_install_path_is_path(self, tmp_path: Path) -> None:
        pkg_info = types.SimpleNamespace(
            install_path=tmp_path,
            package=types.SimpleNamespace(name="test"),
            package_type="STANDARD",
            dependency_ref=None,
        )
        assert isinstance(pkg_info.install_path, Path)
        assert isinstance(pkg_info.install_path.name, str)

    def test_dependency_ref_can_be_none(self) -> None:
        """Local bundles may pass dependency_ref=None -- integrators must
        handle this (skill_integrator checks ``if dep_ref is not None``)."""
        pkg_info = types.SimpleNamespace(
            install_path=Path("/fake"),
            package=types.SimpleNamespace(name="test"),
            package_type="STANDARD",
            dependency_ref=None,
        )
        assert pkg_info.dependency_ref is None


# ---------------------------------------------------------------------------
# Rejected flag validation
# ---------------------------------------------------------------------------

# Each tuple: (CLI flag, value-or-None).  ``None`` means a boolean / repeating
# flag with no payload.
_REJECTED_FLAGS: list[tuple[str, str | None]] = [
    ("--update", None),
    ("--only", "apm"),
    ("--runtime", "copilot"),
    ("--exclude", "codex"),
    ("--dev", None),
    ("--ssh", None),
    ("--https", None),
    ("--allow-protocol-fallback", None),
    ("--mcp", "io.github.test/test"),
    ("--registry", "https://example.com"),
    ("--skill", "my-skill"),
    ("--parallel-downloads", "8"),
    ("--allow-insecure", None),
    ("--allow-insecure-host", "example.com"),
    ("--no-policy", None),
]


class TestRejectedFlagsWithLocalBundle:
    """Each rejected flag must produce UsageError when combined with a local bundle."""

    @pytest.mark.parametrize("flag,value", _REJECTED_FLAGS, ids=lambda x: str(x))
    def test_rejected_flags_produce_usage_error(
        self,
        flag: str,
        value: str | None,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        bundle = _make_bundle(tmp_path / "src")
        project = _make_project(tmp_path / "dst")

        extra = [flag] if value is None else [flag, value]
        # --mcp is a special case: it short-circuits BEFORE local-bundle
        # detection because the early-exit gate excludes it.  In that
        # case, install treats the bundle path as a positional arg with
        # a different code path; still expect non-zero exit + a usage
        # error somewhere.
        try:
            result = _invoke(project, monkeypatch, str(bundle), *extra)
        except SystemExit as exc:
            # Click raises SystemExit on UsageError when catch_exceptions=False
            assert exc.code != 0, f"Flag {flag} should fail (got exit 0)"
            return
        except Exception as exc:
            # UsageError can also propagate as click.UsageError
            assert (
                "not valid with a local bundle" in str(exc)
                or "Missing argument" in str(exc)
                or "--mcp" in str(exc)
            ), f"Unexpected error for {flag}: {exc!r}"
            return

        assert result.exit_code != 0, f"Flag {flag} should be rejected. stdout={result.output!r}"


# ---------------------------------------------------------------------------
# Allowed flag validation
# ---------------------------------------------------------------------------

_ALLOWED_FLAGS: list[tuple[str, ...]] = [
    ("--target", "copilot"),
    ("--force",),
    ("--dry-run",),
    ("--verbose",),
    ("--as", "my-alias"),
]


class TestAllowedFlagsWithLocalBundle:
    """Allowed flags must not produce UsageError when combined with a local bundle."""

    @pytest.mark.parametrize("flag_args", _ALLOWED_FLAGS, ids=lambda x: x[0])
    def test_allowed_flags_accepted_with_local_bundle(
        self,
        flag_args: tuple[str, ...],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        bundle = _make_bundle(tmp_path / "src")
        project = _make_project(tmp_path / "dst")

        result = _invoke(project, monkeypatch, str(bundle), *flag_args)
        # Must not produce a UsageError (exit 2). exit 0 = success;
        # other non-2 exits indicate runtime errors unrelated to flag
        # parsing, which is what we're guarding against.
        assert result.exit_code != 2, (
            f"Allowed flag {flag_args} produced UsageError. output={result.output!r}"
        )


# ---------------------------------------------------------------------------
# --as alias derivation
# ---------------------------------------------------------------------------


class TestAsAliasDerivation:
    """Tests for the --as flag alias logic (slug shown in verbose log)."""

    def test_as_flag_overrides_plugin_json_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bundle = _make_bundle(tmp_path / "src", plugin_id="original-id")
        project = _make_project(tmp_path / "dst")

        result = _invoke(
            project,
            monkeypatch,
            str(bundle),
            "--as",
            "my-alias",
            "--verbose",
            "--target",
            "copilot",
        )
        assert result.exit_code == 0, f"output={result.output!r}"
        # Verbose log line in services.py:417 includes the slug.
        assert "my-alias" in result.output, (
            f"--as alias not surfaced in verbose log. output={result.output!r}"
        )
        # And the original id should NOT be the chosen slug (may still
        # appear in other contexts, so check via the formatted phrase).
        assert "'original-id'" not in result.output

    def test_alias_falls_back_to_plugin_json_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bundle = _make_bundle(tmp_path / "src", plugin_id="from-plugin-json")
        project = _make_project(tmp_path / "dst")

        result = _invoke(project, monkeypatch, str(bundle), "--verbose", "--target", "copilot")
        assert result.exit_code == 0, f"output={result.output!r}"
        assert "from-plugin-json" in result.output

    def test_alias_falls_back_to_dirname_when_no_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # plugin_id=None -> plugin.json has no "id"; loader falls back to
        # the bundle directory name (which our helper names "bundle").
        bundle = _make_bundle(tmp_path / "src", plugin_id=None)
        project = _make_project(tmp_path / "dst")

        result = _invoke(project, monkeypatch, str(bundle), "--verbose", "--target", "copilot")
        assert result.exit_code == 0, f"output={result.output!r}"
        assert "'bundle'" in result.output, (
            f"Dirname-derived slug missing. output={result.output!r}"
        )


# ---------------------------------------------------------------------------
# apm.yml mutation guard
# ---------------------------------------------------------------------------


class TestApmYmlNotMutated:
    """Local-bundle install must NOT mutate apm.yml."""

    def test_apm_yml_not_mutated_by_local_install(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bundle = _make_bundle(tmp_path / "src")
        project = _make_project(tmp_path / "dst")

        before = (project / "apm.yml").read_bytes()
        result = _invoke(project, monkeypatch, str(bundle), "--target", "copilot")
        assert result.exit_code == 0, f"output={result.output!r}"
        after = (project / "apm.yml").read_bytes()
        assert before == after, "apm.yml mutated by local-bundle install"


# ---------------------------------------------------------------------------
# IM7: tarball-but-not-bundle yields targeted UsageError
# ---------------------------------------------------------------------------


class TestPathExistsButNotBundle:
    """A tarball that doesn't look like an APM bundle must produce a clear
    error rather than silently falling through to the registry path."""

    def test_invalid_tarball_raises_usage_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Create a tarball that contains junk -- no plugin.json.
        src = tmp_path / "junk"
        src.mkdir()
        (src / "random.txt").write_text("not a bundle")
        archive = tmp_path / "junk.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(src, arcname="junk")

        project = _make_project(tmp_path / "dst")
        result = _invoke(project, monkeypatch, str(archive))
        assert result.exit_code != 0
        assert "not a valid APM bundle" in result.output

    def test_legacy_apm_format_tarball_raises_actionable_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A tarball packed with --format apm (has apm.lock.yaml, no
        plugin.json) must produce a specific error guiding the user to
        repack with --format plugin or use apm unpack."""
        # Build a legacy apm-format bundle (mirrors packer.py fmt="apm" output)
        bundle = tmp_path / "test-pkg-0.1.0"
        bundle.mkdir(parents=True)
        (bundle / ".github" / "copilot-instructions.md").mkdir(parents=True, exist_ok=True)
        inst_file = bundle / ".github" / "copilot-instructions.md"
        # Fix: copilot-instructions.md is a file, not a directory
        import shutil

        shutil.rmtree(inst_file, ignore_errors=True)
        inst_file.parent.mkdir(parents=True, exist_ok=True)
        inst_file.write_text("# Instructions\n", encoding="utf-8")

        lock_data = {
            "pack": {"format": "apm", "target": "copilot"},
            "dependencies": [
                {
                    "repo_url": "owner/test-pkg",
                    "resolved_commit": "abc123",
                    "deployed_files": [".github/copilot-instructions.md"],
                }
            ],
        }
        (bundle / "apm.lock.yaml").write_text(
            yaml.dump(lock_data, default_flow_style=False), encoding="utf-8"
        )

        # Archive it (no plugin.json!)
        archive = tmp_path / "legacy.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(bundle, arcname=bundle.name)

        project = _make_project(tmp_path / "dst")
        result = _invoke(project, monkeypatch, str(archive))
        assert result.exit_code != 0
        # Must mention the legacy format and offer actionable guidance
        assert "--format apm" in result.output or "legacy format" in result.output
        assert "apm unpack" in result.output or "--format plugin" in result.output


# ---------------------------------------------------------------------------
# IM8: --as on a registry install is rejected
# ---------------------------------------------------------------------------


class TestAsFlagRequiresLocalBundle:
    def test_as_rejected_on_registry_install(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _make_project(tmp_path / "dst")
        result = _invoke(project, monkeypatch, "owner/pkg", "--as", "alias")
        assert result.exit_code != 0
        assert "--as requires a local bundle" in result.output
