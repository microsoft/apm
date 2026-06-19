"""Tests for git-semver-resolution fields on ``LockedDependency``."""

from __future__ import annotations

from apm_cli.deps.git_semver_resolver import GitSemverResolution
from apm_cli.deps.installed_package import InstalledPackage
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.dependency.reference import DependencyReference


def _make_dep_ref(repo_url: str = "acme/some-skills", reference: str = "^1.2.0"):
    return DependencyReference(
        repo_url=repo_url,
        host="github.com",
        reference=reference,
        source="git",
    )


def _make_resolution(constraint: str = "^1.2.0") -> GitSemverResolution:
    return GitSemverResolution(
        constraint=constraint,
        resolved_version="1.5.3",
        resolved_tag="v1.5.3",
        resolved_sha="c" * 40,
        matched_pattern="v{version}",
        resolved_at="2024-06-15T12:00:00+00:00",
    )


class TestLockedDependencySerialization:
    """Resolution fields round-trip through to_dict / from_dict."""

    def test_git_semver_dep_writes_constraint_and_resolved_tag_fields(self) -> None:
        dep_ref = _make_dep_ref()
        resolution = _make_resolution()

        locked = LockedDependency.from_dependency_ref(
            dep_ref=dep_ref,
            resolved_commit=resolution.resolved_sha,
            depth=1,
            resolved_by=None,
            git_semver_resolution=resolution,
        )

        # All three new fields are populated on the dataclass.
        assert locked.constraint == "^1.2.0"
        assert locked.resolved_tag == "v1.5.3"
        assert locked.resolved_at == "2024-06-15T12:00:00+00:00"
        # The concrete tag becomes ``resolved_ref`` so re-installs route through
        # the literal-tag git path, NOT the original semver range.
        assert locked.resolved_ref == "v1.5.3"
        # Version is recorded too (so audits can answer "what version is locked?").
        assert locked.version == "1.5.3"

        d = locked.to_dict()
        assert d["constraint"] == "^1.2.0"
        assert d["resolved_tag"] == "v1.5.3"
        assert d["resolved_at"] == "2024-06-15T12:00:00+00:00"
        assert d["resolved_ref"] == "v1.5.3"
        assert d["version"] == "1.5.3"

    def test_git_semver_lockfile_roundtrips_through_to_dict_from_dict(self) -> None:
        dep_ref = _make_dep_ref()
        resolution = _make_resolution()
        locked = LockedDependency.from_dependency_ref(
            dep_ref=dep_ref,
            resolved_commit=resolution.resolved_sha,
            depth=1,
            resolved_by=None,
            git_semver_resolution=resolution,
        )

        rebuilt = LockedDependency.from_dict(locked.to_dict())

        assert rebuilt.constraint == locked.constraint
        assert rebuilt.resolved_tag == locked.resolved_tag
        assert rebuilt.resolved_at == locked.resolved_at
        assert rebuilt.resolved_ref == locked.resolved_ref
        assert rebuilt.version == locked.version
        # Going back through to_dict produces the identical mapping.
        assert rebuilt.to_dict() == locked.to_dict()

    def test_dep_with_no_resolution_omits_git_semver_fields(self) -> None:
        # Plain ref (branch / literal tag / SHA) must not introduce
        # empty ``constraint`` keys -- those would dirty existing lockfiles.
        dep_ref = _make_dep_ref(reference="main")
        locked = LockedDependency.from_dependency_ref(
            dep_ref=dep_ref,
            resolved_commit="a" * 40,
            depth=1,
            resolved_by=None,
        )

        d = locked.to_dict()
        assert "constraint" not in d
        assert "resolved_tag" not in d
        assert "resolved_at" not in d
        # The legacy resolved_ref still tracks the manifest ref.
        assert d["resolved_ref"] == "main"


class TestLockfileVersion:
    """Lockfile version remains v2 after git-semver resolution."""

    def test_lockfile_version_remains_v2_after_git_semver_resolution(self) -> None:
        dep_ref = _make_dep_ref()
        resolution = _make_resolution()
        lock = LockFile()
        locked = LockedDependency.from_dependency_ref(
            dep_ref=dep_ref,
            resolved_commit=resolution.resolved_sha,
            depth=1,
            resolved_by=None,
            git_semver_resolution=resolution,
        )
        lock.add_dependency(locked)

        yaml_str = lock.to_yaml()
        # Bumping the schema for an optional, forward-compatible field
        # would force a "lockfile version mismatch" warning across every
        # existing lockfile in the wild. v2 stays.
        assert "lockfile_version: '2'" in yaml_str or 'lockfile_version: "2"' in yaml_str

        rebuilt = LockFile.from_yaml(yaml_str)
        assert rebuilt.lockfile_version == "2"

    def test_lockfile_version_stays_v1_when_only_git_deps_have_no_semver(self) -> None:
        # Resolution fields are forward-compat additions; their *presence*
        # alone must not trigger a v2 bump for projects that only use
        # plain git deps with literal refs.
        dep_ref = _make_dep_ref(reference="v1.0.0")
        lock = LockFile()
        locked = LockedDependency.from_dependency_ref(
            dep_ref=dep_ref,
            resolved_commit="a" * 40,
            depth=1,
            resolved_by=None,
        )
        lock.add_dependency(locked)

        yaml_str = lock.to_yaml()
        assert "lockfile_version: '1'" in yaml_str or 'lockfile_version: "1"' in yaml_str


class TestForwardCompatUnknownKeys:
    """Forward-compat trap: unknown keys survive a round-trip."""

    def test_lockfile_preserves_unknown_keys_on_roundtrip(self) -> None:
        # Simulate an older APM build reading a lockfile written by a
        # newer build with future fields. The fields must NOT be silently
        # dropped on re-emit.
        future_payload = {
            "repo_url": "acme/some-skills",
            "host": "github.com",
            "resolved_commit": "c" * 40,
            "resolved_ref": "v1.5.3",
            "constraint": "^1.2.0",
            "resolved_tag": "v1.5.3",
            "future_field_we_dont_know": "some-value",
            "another_future_dict": {"nested": True},
        }

        locked = LockedDependency.from_dict(future_payload)
        emitted = locked.to_dict()

        assert emitted["future_field_we_dont_know"] == "some-value"
        assert emitted["another_future_dict"] == {"nested": True}
        # Known fields still serialize correctly.
        assert emitted["constraint"] == "^1.2.0"
        assert emitted["resolved_tag"] == "v1.5.3"


class TestInstalledPackagePlumbing:
    """``from_installed_packages`` propagates the resolution through."""

    def test_installed_package_carries_resolution_into_lockfile(self) -> None:
        dep_ref = _make_dep_ref()
        resolution = _make_resolution()
        pkg = InstalledPackage(
            dep_ref=dep_ref,
            resolved_commit=resolution.resolved_sha,
            depth=1,
            resolved_by=None,
            git_semver_resolution=resolution,
        )

        lock = LockFile.from_installed_packages(
            installed_packages=[pkg],
            dependency_graph=None,  # unused by from_installed_packages
        )

        deps = lock.get_all_dependencies()
        assert len(deps) == 1
        ld = deps[0]
        assert ld.constraint == "^1.2.0"
        assert ld.resolved_tag == "v1.5.3"
        assert ld.resolved_ref == "v1.5.3"
        assert ld.version == "1.5.3"


class TestMutualExclusivity:
    """``from_dependency_ref`` enforces resolution-source mutual exclusivity.

    Regression-trap for PR #1496 review thread: the docstring promises
    ``git_semver_resolution`` is mutually exclusive with
    ``registry_resolution``, but the constructor previously combined
    fields from both (e.g. ``source="registry"`` while also setting
    ``constraint``/``resolved_tag`` and overriding ``resolved_ref``).
    """

    def test_passing_both_resolution_sources_raises_value_error(self) -> None:
        from apm_cli.deps.registry.resolver import RegistryResolution

        dep_ref = _make_dep_ref()
        git_res = _make_resolution()
        reg_res = RegistryResolution(
            resolved_url="https://registry.example/pkg/1.0.0.tgz",
            resolved_hash="sha256-abc",
            version="1.0.0",
        )

        import pytest

        with pytest.raises(ValueError, match=r"mutually exclusive"):
            LockedDependency.from_dependency_ref(
                dep_ref=dep_ref,
                resolved_commit="d" * 40,
                depth=1,
                resolved_by=None,
                registry_resolution=reg_res,
                git_semver_resolution=git_res,
            )
