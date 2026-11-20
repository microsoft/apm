"""Tests for policy.fetch_failure schema knob and project-side (filesystem, process and CLI boundary cases)."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any, List  # noqa: F401, UP035
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.policy.discovery import PolicyFetchResult
from apm_cli.policy.project_config import read_project_fetch_failure_default

pytestmark = pytest.mark.component


# =====================================================================
# Parser: validates fetch_failure
# =====================================================================


# =====================================================================
# Project-side fetch_failure_default reader
# =====================================================================


class TestProjectFetchFailureDefault:
    def test_missing_apm_yml_returns_warn(self, tmp_path: Path):
        assert read_project_fetch_failure_default(tmp_path) == "warn"

    def test_apm_yml_without_policy_block_returns_warn(self, tmp_path: Path):
        (tmp_path / "apm.yml").write_text("name: p\nversion: '1.0'\n", encoding="utf-8")
        assert read_project_fetch_failure_default(tmp_path) == "warn"

    def test_explicit_block(self, tmp_path: Path):
        (tmp_path / "apm.yml").write_text(
            textwrap.dedent("""\
                name: p
                version: '1.0'
                policy:
                  fetch_failure_default: block
            """),
            encoding="utf-8",
        )
        assert read_project_fetch_failure_default(tmp_path) == "block"

    def test_explicit_warn(self, tmp_path: Path):
        (tmp_path / "apm.yml").write_text(
            textwrap.dedent("""\
                name: p
                version: '1.0'
                policy:
                  fetch_failure_default: warn
            """),
            encoding="utf-8",
        )
        assert read_project_fetch_failure_default(tmp_path) == "warn"

    def test_garbage_value_falls_back_to_warn(self, tmp_path: Path):
        (tmp_path / "apm.yml").write_text(
            textwrap.dedent("""\
                name: p
                version: '1.0'
                policy:
                  fetch_failure_default: bogus
            """),
            encoding="utf-8",
        )
        assert read_project_fetch_failure_default(tmp_path) == "warn"

    def test_malformed_yaml_returns_warn(self, tmp_path: Path):
        (tmp_path / "apm.yml").write_text(":::not yaml:::\n", encoding="utf-8")
        assert read_project_fetch_failure_default(tmp_path) == "warn"


# =====================================================================
# policy_gate: fail-closed behaviour
# =====================================================================


def _fetch(outcome, *, policy=None, source="org:contoso/.github", fetch_error=None, error=None):
    return PolicyFetchResult(
        policy=policy,
        source=source,
        cached=False,
        error=error,
        cache_age_seconds=None,
        cache_stale=outcome == "cached_stale",
        fetch_error=fetch_error,
        outcome=outcome,
    )


# =====================================================================
# install_preflight parallel call site
# =====================================================================


class TestPreflightFailClosed:
    @patch("apm_cli.policy.install_preflight.discover_policy_with_chain")
    def test_block_raises_PolicyBlockError(self, mock_discover, tmp_path: Path):
        from apm_cli.policy.install_preflight import (
            PolicyBlockError,
            run_policy_preflight,
        )

        mock_discover.return_value = _fetch("cache_miss_fetch_fail", fetch_error="dns fail")
        (tmp_path / "apm.yml").write_text(
            "name: p\nversion: '1.0'\npolicy:\n  fetch_failure_default: block\n",
            encoding="utf-8",
        )
        with pytest.raises(PolicyBlockError):
            run_policy_preflight(
                project_root=tmp_path,
                apm_deps=[],
                no_policy=False,
                logger=MagicMock(),
            )

    @patch("apm_cli.policy.install_preflight.discover_policy_with_chain")
    def test_warn_does_not_raise(self, mock_discover, tmp_path: Path):
        from apm_cli.policy.install_preflight import run_policy_preflight

        mock_discover.return_value = _fetch("cache_miss_fetch_fail", fetch_error="dns fail")
        # No apm.yml -> default warn.
        result, active = run_policy_preflight(  # noqa: RUF059
            project_root=tmp_path,
            apm_deps=[],
            no_policy=False,
            logger=MagicMock(),
        )
        assert active is False

    @patch("apm_cli.policy.install_preflight.discover_policy_with_chain")
    def test_dry_run_block_does_not_raise(self, mock_discover, tmp_path: Path):
        from apm_cli.policy.install_preflight import run_policy_preflight

        mock_discover.return_value = _fetch("cache_miss_fetch_fail", fetch_error="dns fail")
        (tmp_path / "apm.yml").write_text(
            "name: p\nversion: '1.0'\npolicy:\n  fetch_failure_default: block\n",
            encoding="utf-8",
        )
        # dry_run never raises.
        result, active = run_policy_preflight(  # noqa: RUF059
            project_root=tmp_path,
            apm_deps=[],
            no_policy=False,
            logger=MagicMock(),
            dry_run=True,
        )
        assert active is False
