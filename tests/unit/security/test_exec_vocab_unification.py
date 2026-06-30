"""Tests for the unified executables vocabulary layer (issue #1873).

Covers:
- ``parse_project_executables`` (new ``executables:{allow,deny}`` block +
  the deprecated ``allowExecutables`` alias).
- The user-local consent store on ``~/.apm/config.json`` plus the one-time
  migration that folds and DELETES the legacy ``~/.apm/approvals.yml``.
- ``build_exec_trust_context`` assembling org / project / user inputs.
"""

from __future__ import annotations

import apm_cli.security.executables as ex
from apm_cli.policy.schema import ApmPolicy, BinDeployPolicy, ExecutablesPolicy
from apm_cli.security.executables import (
    EXEC_TYPE_BIN,
    EXEC_TYPE_HOOKS,
    build_exec_trust_context,
    load_user_executables,
    parse_project_executables,
    save_user_executables,
)


class TestParseProjectExecutables:
    def test_absent_returns_empty_no_deprecation(self):
        allow, deny, deprecated = parse_project_executables({})
        assert allow == {}
        assert deny == {}
        assert deprecated is False

    def test_new_block_allow_and_deny(self):
        data = {
            "executables": {
                "allow": {"owner/repo": {"hooks": True}},
                "deny": {"bad/pkg": {"bin": True}},
            }
        }
        allow, deny, deprecated = parse_project_executables(data)
        assert allow == {"owner/repo": {"hooks": True}}
        assert deny == {"bad/pkg": {"bin": True}}
        assert deprecated is False

    def test_allow_executables_alias_sets_deprecation_flag(self):
        data = {"allowExecutables": {"owner/repo": {"hooks": True}}}
        allow, _deny, deprecated = parse_project_executables(data)
        assert allow == {"owner/repo": {"hooks": True}}
        assert deprecated is True

    def test_new_block_wins_over_alias_on_conflict(self):
        data = {
            "allowExecutables": {"owner/repo": {"hooks": False}},
            "executables": {"allow": {"owner/repo": {"hooks": True}}},
        }
        allow, _deny, deprecated = parse_project_executables(data)
        assert allow["owner/repo"]["hooks"] is True
        assert deprecated is True


class TestUserExecutablesStore:
    def test_roundtrip_via_config(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        monkeypatch.setattr(ex, "_user_config_file", lambda: cfg)
        monkeypatch.setattr(ex, "_legacy_approvals_path", lambda: tmp_path / "approvals.yml")

        save_user_executables({"owner/repo": {"hooks": True}}, {"bad/pkg": {"bin": True}})
        allow, deny = load_user_executables()
        assert allow == {"owner/repo": {"hooks": True}}
        assert deny == {"bad/pkg": {"bin": True}}

    def test_migrates_and_deletes_legacy_approvals_yml(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        legacy = tmp_path / "approvals.yml"
        monkeypatch.setattr(ex, "_user_config_file", lambda: cfg)
        monkeypatch.setattr(ex, "_legacy_approvals_path", lambda: legacy)

        from apm_cli.utils.yaml_io import dump_yaml

        dump_yaml({"owner/repo": {"hooks": True}}, legacy)
        assert legacy.exists()

        allow, _deny = load_user_executables()
        # Legacy approvals folded into the allow set.
        assert allow.get("owner/repo", {}).get("hooks") is True
        # net-new control-surface files = 0: the legacy file is removed.
        assert not legacy.exists()


class TestBuildExecTrustContext:
    def test_org_executables_block_enables_gate_fleetwide(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ex, "_user_config_file", lambda: tmp_path / "c.json")
        monkeypatch.setattr(ex, "_legacy_approvals_path", lambda: tmp_path / "a.yml")
        policy = ApmPolicy(executables=ExecutablesPolicy(deny=("bad/pkg",)))
        ctx = build_exec_trust_context(policy=policy, project_data={})
        assert ctx.gate_enabled is True
        assert "bad/pkg" in ctx.org_deny

    def test_legacy_bin_deploy_maps_to_bin_deny(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ex, "_user_config_file", lambda: tmp_path / "c.json")
        monkeypatch.setattr(ex, "_legacy_approvals_path", lambda: tmp_path / "a.yml")
        policy = ApmPolicy(bin_deploy=BinDeployPolicy(deny=("https://github.com/BAD/PKG.git",)))
        ctx = build_exec_trust_context(policy=policy, project_data={})
        assert "bad/pkg" in ctx.org_bin_deny

    def test_project_block_enables_gate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ex, "_user_config_file", lambda: tmp_path / "c.json")
        monkeypatch.setattr(ex, "_legacy_approvals_path", lambda: tmp_path / "a.yml")
        data = {"executables": {"allow": {"owner/repo": {"hooks": True}}}}
        ctx = build_exec_trust_context(policy=None, project_data=data)
        assert ctx.gate_enabled is True
        assert ctx.project_allow == {"owner/repo": {"hooks": True}}

    def test_no_signals_gate_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ex, "_user_config_file", lambda: tmp_path / "c.json")
        monkeypatch.setattr(ex, "_legacy_approvals_path", lambda: tmp_path / "a.yml")
        ctx = build_exec_trust_context(policy=None, project_data={})
        assert ctx.gate_enabled is False

    def test_user_consent_flows_into_context(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ex, "_user_config_file", lambda: tmp_path / "c.json")
        monkeypatch.setattr(ex, "_legacy_approvals_path", lambda: tmp_path / "a.yml")
        save_user_executables({"owner/repo": {EXEC_TYPE_HOOKS: True}}, {})
        data = {"executables": {"allow": {}}}
        ctx = build_exec_trust_context(policy=None, project_data=data)
        assert ctx.user_allow.get("owner/repo", {}).get(EXEC_TYPE_HOOKS) is True
        assert EXEC_TYPE_BIN not in ctx.user_allow.get("owner/repo", {})
