"""Hermetic ``apm install`` reproduction for #3167: nested hook metadata.

A dependency whose hook handler carries a nested ``env`` object failed to
integrate with ``Object of type mappingproxy is not JSON serializable``. This
drives the real ``apm install`` CLI with only the download seam stubbed, so
no network access is needed and no hook is executed.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.models.apm_package import (
    APMPackage,
    GitReferenceType,
    PackageInfo,
    ResolvedReference,
    clear_apm_yml_cache,
)
from apm_cli.models.dependency.reference import DependencyReference

pytestmark = [pytest.mark.integration]

_ENV = {"APM_TEST": "value", "NESTED": {"LIST": ["a", {"b": 1}]}}
_HANDLER = {"type": "command", "command": "./scripts/check.sh", "env": _ENV}


def _download(
    _self: GitHubPackageDownloader,
    repo_ref: object,
    install_path: Path,
    *_args: object,
    **_kwargs: object,
) -> PackageInfo:
    dep_ref = (
        repo_ref
        if isinstance(repo_ref, DependencyReference)
        else DependencyReference.parse(str(repo_ref))
    )
    install_path = Path(install_path)
    hooks_dir = install_path / ".apm" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (install_path / "apm.yml").write_text(
        yaml.safe_dump({"name": "nested-env", "version": "1.0.0"}), encoding="utf-8"
    )
    (hooks_dir / "pre.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [_HANDLER]}]}}),
        encoding="utf-8",
    )
    return PackageInfo(
        package=APMPackage.from_apm_yml(install_path / "apm.yml"),
        install_path=install_path,
        installed_at=datetime.now().isoformat(),
        dependency_ref=dep_ref,
        resolved_reference=ResolvedReference(
            original_ref="main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit=None,
            ref_name="main",
        ),
    )


def test_install_writes_nested_hook_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": "nested-env-consumer",
                "version": "1.0.0",
                "targets": ["claude", "codex"],
                "dependencies": {"apm": ["acme/nested-env"]},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    clear_apm_yml_cache()
    monkeypatch.chdir(project)

    with (
        patch("apm_cli.commands._helpers.check_for_updates", return_value=None),
        patch.object(
            GitHubPackageDownloader, "download_package", autospec=True, side_effect=_download
        ),
    ):
        result = CliRunner().invoke(cli, ["install", "--no-policy"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert "mappingproxy" not in result.output
    for config in (".claude/settings.json", ".codex/hooks.json"):
        document = json.loads((project / config).read_text(encoding="utf-8"))
        [entry] = document["hooks"]["PreToolUse"]
        assert entry["matcher"] == "Bash", config
        [handler] = entry["hooks"]
        assert handler["env"] == _ENV, config
