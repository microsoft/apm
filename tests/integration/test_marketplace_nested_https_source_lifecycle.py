"""Installed-binary lifecycle contract for nested HTTPS marketplace sources."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.lifecycle_merge_group,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_NESTED_SOURCE = "https://git.example.invalid/group/subgroup/marketplace-package.git"
_UNSAFE_NESTED_SOURCE = "https://git.example.invalid/group/%2e%2e/marketplace-package.git"
_ENCODED_ADO_BASE = "https://dev.azure.com/contoso/My%20Projects/_git"
_ENCODED_ADO_SOURCE = f"{_ENCODED_ADO_BASE}/agent-skills"
_SHA = "a" * 40


def _write_marketplace_config(
    project: Path,
    source: str,
    *,
    source_base: str | None = None,
    ref: str = "v1.0.0",
) -> None:
    """Write a minimal marketplace manifest with one remote package."""
    project.mkdir()
    source_base_line = f"  sourceBase: {source_base}\n" if source_base else ""
    (project / "apm.yml").write_text(
        f"""\
name: nested-marketplace
description: Nested HTTPS marketplace source lifecycle test
version: 1.0.0
marketplace:
  owner:
    name: Test Owner
{source_base_line}  packages:
    - name: nested-package
      source: {source}
      ref: {ref}
""",
        encoding="utf-8",
    )


def test_marketplace_check_offline_reaches_nested_https_ref_resolution_without_writes(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A nested HTTPS source reaches offline resolution without project mutation."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    project = isolated.work_root / "nested-marketplace"
    _write_marketplace_config(project, _NESTED_SOURCE)
    before = ArtifactSnapshot.capture(project)
    runner = ApmLifecycleRunner((str(apm_binary_path),))

    (result,) = runner.run_sequence(
        (("marketplace", "check", "--offline", "--verbose"),),
        expected_returncodes=(1,),
        scenario_id="marketplace-nested-https-check",
        cwd=project,
        env=isolated.subprocess_env(overrides={"COLUMNS": "240"}),
    )

    diagnostics = result.stdout + result.stderr
    assert "marketplace config error" not in diagnostics
    assert "No cached refs (offline)" in diagnostics
    expected_source = urlparse(_NESTED_SOURCE)
    diagnostic_urls = [
        urlparse(token.strip("(),.;'\"")) for token in diagnostics.split() if "://" in token
    ]
    assert any(
        url.scheme == expected_source.scheme
        and url.hostname == expected_source.hostname
        and url.path == expected_source.path
        for url in diagnostic_urls
    )
    assert_unchanged(before, ArtifactSnapshot.capture(project))


def test_marketplace_check_offline_rejects_unsafe_nested_https_source_before_ref_lookup(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Unsafe nested HTTPS paths fail validation before offline ref resolution."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    project = isolated.work_root / "unsafe-nested-marketplace"
    _write_marketplace_config(project, _UNSAFE_NESTED_SOURCE)
    before = ArtifactSnapshot.capture(project)
    runner = ApmLifecycleRunner((str(apm_binary_path),))

    (result,) = runner.run_sequence(
        (("marketplace", "check", "--offline", "--verbose"),),
        expected_returncodes=(2,),
        scenario_id="marketplace-nested-https-check",
        cwd=project,
        env=isolated.subprocess_env(overrides={"COLUMNS": "240"}),
    )

    diagnostics = result.stdout + result.stderr
    assert "marketplace config error" in diagnostics
    assert "No cached refs (offline)" not in diagnostics
    assert_unchanged(before, ArtifactSnapshot.capture(project))


@pytest.mark.parametrize(
    ("source_base", "source", "expected_url"),
    [
        (_ENCODED_ADO_BASE, "agent-skills", _ENCODED_ADO_SOURCE),
        (None, _ENCODED_ADO_SOURCE, _ENCODED_ADO_SOURCE),
    ],
)
def test_pack_offline_dry_run_preserves_encoded_ado_source_without_writes(
    tmp_path: Path,
    apm_binary_path: Path,
    source_base: str | None,
    source: str,
    expected_url: str,
) -> None:
    """Both source forms pack offline without changing project bytes."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    project = isolated.work_root / "encoded-ado-marketplace"
    _write_marketplace_config(project, source, source_base=source_base, ref=_SHA)
    before = ArtifactSnapshot.capture(project)
    runner = ApmLifecycleRunner((str(apm_binary_path),))

    (result,) = runner.run_sequence(
        (("pack", "--offline", "--dry-run", "--verbose"),),
        expected_returncodes=(0,),
        scenario_id="marketplace-encoded-ado-pack",
        cwd=project,
        env=isolated.subprocess_env(overrides={"COLUMNS": "240"}),
    )

    assert result.returncode == 0
    manifest = load_yaml(project / "apm.yml")
    marketplace = manifest["marketplace"]
    if source_base is not None:
        assert urlparse(marketplace["sourceBase"]).path == urlparse(_ENCODED_ADO_BASE).path
    else:
        assert urlparse(marketplace["packages"][0]["source"]).path == urlparse(expected_url).path
    assert_unchanged(before, ArtifactSnapshot.capture(project))
