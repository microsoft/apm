"""Unit tests for commands.install.InstallContext dataclass.

Covers P1-G1: the CLI parameter-bundle dataclass introduced in WI-3 has
zero test coverage.  These tests verify the structural contract (dataclass
annotation, field names, defaults) and basic round-trip construction.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import MagicMock, sentinel

import pytest

from apm_cli.commands.install import InstallContext


# ---------------------------------------------------------------------------
# P1-G1 -- InstallContext dataclass structural tests
# ---------------------------------------------------------------------------


class TestInstallContextIsDataclass:
    """InstallContext must be a @dataclasses.dataclass."""

    def test_is_dataclass(self):
        assert dataclasses.is_dataclass(InstallContext), (
            "InstallContext must be decorated with @dataclasses.dataclass"
        )

    def test_is_not_frozen(self):
        """The CLI context is mutable (snapshot fields are set after construction)."""
        assert not InstallContext.__dataclass_params__.frozen


class TestInstallContextFields:
    """All expected fields must be present with correct names."""

    EXPECTED_FIELDS = (
        "scope",
        "manifest_path",
        "manifest_display",
        "apm_dir",
        "project_root",
        "logger",
        "auth_resolver",
        "verbose",
        "force",
        "dry_run",
        "update",
        "dev",
        "runtime",
        "exclude",
        "target",
        "parallel_downloads",
        "allow_insecure",
        "allow_insecure_hosts",
        "protocol_pref",
        "allow_protocol_fallback",
        "trust_transitive_mcp",
        "no_policy",
        "install_mode",
        "packages",
        # optional (default=None)
        "only_packages",
        "manifest_snapshot",
        "snapshot_manifest_path",
    )

    def test_all_required_fields_present(self):
        field_names = tuple(f.name for f in dataclasses.fields(InstallContext))
        for name in self.EXPECTED_FIELDS:
            assert name in field_names, f"Missing field: {name}"

    def test_no_unexpected_fields(self):
        field_names = set(f.name for f in dataclasses.fields(InstallContext))
        expected = set(self.EXPECTED_FIELDS)
        unexpected = field_names - expected
        assert not unexpected, f"Unexpected fields: {unexpected}"

    def test_field_count_matches(self):
        actual = len(dataclasses.fields(InstallContext))
        assert actual == len(self.EXPECTED_FIELDS), (
            f"Expected {len(self.EXPECTED_FIELDS)} fields, got {actual}"
        )


class TestInstallContextDefaults:
    """Optional fields must default to None."""

    def _build_minimal(self, **overrides):
        """Construct InstallContext with sentinel values for required fields."""
        defaults = dict(
            scope=sentinel.SCOPE,
            manifest_path=Path("/tmp/apm.yml"),
            manifest_display="apm.yml",
            apm_dir=Path("/tmp/apm_modules"),
            project_root=Path("/tmp"),
            logger=MagicMock(),
            auth_resolver=MagicMock(),
            verbose=False,
            force=False,
            dry_run=False,
            update=False,
            dev=False,
            runtime=None,
            exclude=None,
            target=None,
            parallel_downloads=4,
            allow_insecure=False,
            allow_insecure_hosts=(),
            protocol_pref=sentinel.PROTO,
            allow_protocol_fallback=False,
            trust_transitive_mcp=False,
            no_policy=False,
            install_mode=sentinel.MODE,
            packages=(),
        )
        defaults.update(overrides)
        return InstallContext(**defaults)

    def test_only_packages_defaults_to_none(self):
        ctx = self._build_minimal()
        assert ctx.only_packages is None

    def test_manifest_snapshot_defaults_to_none(self):
        ctx = self._build_minimal()
        assert ctx.manifest_snapshot is None

    def test_snapshot_manifest_path_defaults_to_none(self):
        ctx = self._build_minimal()
        assert ctx.snapshot_manifest_path is None


class TestInstallContextRoundTrip:
    """Constructing with sentinel values and reading them back works."""

    def test_round_trip_required_fields(self):
        ctx = InstallContext(
            scope=sentinel.SCOPE,
            manifest_path=Path("/proj/apm.yml"),
            manifest_display="apm.yml",
            apm_dir=Path("/proj/apm_modules"),
            project_root=Path("/proj"),
            logger=sentinel.LOGGER,
            auth_resolver=sentinel.AUTH,
            verbose=True,
            force=True,
            dry_run=True,
            update=True,
            dev=True,
            runtime="copilot",
            exclude="tests",
            target="copilot",
            parallel_downloads=8,
            allow_insecure=True,
            allow_insecure_hosts=("mirror.example.com",),
            protocol_pref=sentinel.PROTO,
            allow_protocol_fallback=True,
            trust_transitive_mcp=True,
            no_policy=True,
            install_mode=sentinel.MODE,
            packages=("owner/repo",),
        )

        assert ctx.scope is sentinel.SCOPE
        assert ctx.manifest_path == Path("/proj/apm.yml")
        assert ctx.manifest_display == "apm.yml"
        assert ctx.apm_dir == Path("/proj/apm_modules")
        assert ctx.project_root == Path("/proj")
        assert ctx.logger is sentinel.LOGGER
        assert ctx.auth_resolver is sentinel.AUTH
        assert ctx.verbose is True
        assert ctx.force is True
        assert ctx.dry_run is True
        assert ctx.update is True
        assert ctx.dev is True
        assert ctx.runtime == "copilot"
        assert ctx.exclude == "tests"
        assert ctx.target == "copilot"
        assert ctx.parallel_downloads == 8
        assert ctx.allow_insecure is True
        assert ctx.allow_insecure_hosts == ("mirror.example.com",)
        assert ctx.protocol_pref is sentinel.PROTO
        assert ctx.allow_protocol_fallback is True
        assert ctx.trust_transitive_mcp is True
        assert ctx.no_policy is True
        assert ctx.install_mode is sentinel.MODE
        assert ctx.packages == ("owner/repo",)

    def test_round_trip_optional_fields(self):
        ctx = InstallContext(
            scope=sentinel.SCOPE,
            manifest_path=Path("/proj/apm.yml"),
            manifest_display="apm.yml",
            apm_dir=Path("/proj/apm_modules"),
            project_root=Path("/proj"),
            logger=sentinel.LOGGER,
            auth_resolver=sentinel.AUTH,
            verbose=False,
            force=False,
            dry_run=False,
            update=False,
            dev=False,
            runtime=None,
            exclude=None,
            target=None,
            parallel_downloads=4,
            allow_insecure=False,
            allow_insecure_hosts=(),
            protocol_pref=sentinel.PROTO,
            allow_protocol_fallback=False,
            trust_transitive_mcp=False,
            no_policy=False,
            install_mode=sentinel.MODE,
            packages=(),
            only_packages=["pkg-a"],
            manifest_snapshot=b"raw-yml-bytes",
            snapshot_manifest_path=Path("/proj/apm.yml"),
        )

        assert ctx.only_packages == ["pkg-a"]
        assert ctx.manifest_snapshot == b"raw-yml-bytes"
        assert ctx.snapshot_manifest_path == Path("/proj/apm.yml")
