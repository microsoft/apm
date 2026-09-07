"""Executable trust routing tests for the install template."""

from __future__ import annotations

from types import SimpleNamespace

import apm_cli.security.executables as ex
from apm_cli.install.template import _effective_allow


def test_effective_allow_reads_source_manifest_under_root_redirect(tmp_path, monkeypatch):
    source_root = tmp_path / "source"
    deploy_root = tmp_path / "deploy"
    source_root.mkdir()
    deploy_root.mkdir()
    (source_root / "apm.yml").write_text("executables:\n  allow: {}\n", encoding="utf-8")
    monkeypatch.setattr(ex, "_user_config_file", lambda: tmp_path / "config.json")
    monkeypatch.setattr(ex, "_legacy_approvals_path", lambda: tmp_path / "approvals.yml")
    ctx = SimpleNamespace(
        project_root=deploy_root,
        source_root=source_root,
        policy_fetch=None,
        apm_package=SimpleNamespace(allow_executables=None),
        exec_trust_ctx=None,
        exec_allow_map=None,
        dry_run=False,
        logger=None,
    )

    assert _effective_allow(ctx) == {}
