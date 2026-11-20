"""Native plugin activation profile conformance -- sec.8.5.11 (amendment 0.1.65).

Each requirement test pins its normative clause in the spec text. The
Claude user-scope route (#3290) binds req-na-002 to req-na-005 and
req-na-007 to real behavior; project scope with Copilot (#3921) and the
Codex split fallback (#3920) still carry waivers naming their delivery.

The req-na-004 oracle is checked against a small reference model of the
reconciliation rule so the fixture stays self-consistent before a
consumer runs against it.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from apm_cli.agent_plugins.errors import AgentPluginDeploymentBoundaryError
from apm_cli.claude_plugins.admission import (
    ClaudeNativePluginError,
    fingerprint_native_claude_source,
)
from apm_cli.claude_plugins.registrar import (
    marketplace_binding,
    marketplace_name,
    native_ownership,
    reconcile_native_plugins,
    verify_claude_native_cache,
)
from apm_cli.core.scope import InstallScope
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.native_plugins.record import NativePluginRecord
from apm_cli.native_plugins.settings import (
    NativeSettingsError,
    SettingsDeclaration,
    apply_declaration,
    write_declaration,
)
from apm_cli.utils.content_hash import compute_native_plugin_hash
from tests.spec_conformance._helpers import assert_spec_contains, load_json_fixture, waive

_ORACLE = ("native-activation", "req-na-004.json")


@pytest.mark.req("req-na-001")
def test_native_activation_requires_documented_part_a_qualification():
    assert_spec_contains(
        "that natively\nactivates plugins MUST do so only for a target",
        "A target **qualifies** for a scope (project or user) and a client version",
        "A target whose qualification\nis unknown does not qualify.",
        "the oldest client\nversion it qualified, and the published source it relies on for each\ncriterion.",
    )
    waive(
        "The Claude user-scope route (#3290) qualifies on its human runtime witness; "
        "its conformance-statement disclosure follows that witness."
    )


_KNOWN_ANSWER = "sha256:45ce9c55a29eafc7734f0008654420d12da9a4f9539ce811592b297c3e92ff3e"


def _known_answer_tree(root: Path) -> Path:
    """Three plugin files plus ``apm.yml``, ``.apm/`` and ``.git`` that never count."""
    for relative, body, mode in (
        ("a.txt", b"alpha\n", 0o644),
        ("bin/run", b"#!/bin/sh\necho run\n", 0o755),
        ("skills/s/SKILL.md", b"---\nname: s\n---\nbody\r\n", 0o644),
        ("apm.yml", b"name: x\n", 0o644),
        (".apm/hooks/h.json", b"{}", 0o644),
        (".git/HEAD", b"ref\n", 0o644),
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        path.chmod(mode)
    return root


class _Home:
    """An isolated user home with apm_modules, a lock and Claude settings."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.home = root / "home"
        self.modules = self.home / ".apm/apm_modules"
        self.settings = self.home / ".claude/settings.json"
        self.plugins = self.home / ".agents/plugins"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(self.home / ".claude"))
        monkeypatch.setenv("HOME", str(self.home))
        monkeypatch.setenv("USERPROFILE", str(self.home))
        self.lock = LockFile()

    def plugin(self, repo: str, name: str) -> Path:
        root = self.modules / repo
        (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        (root / ".claude-plugin/plugin.json").write_text(json.dumps({"name": name}))
        (root / "commands").mkdir(exist_ok=True)
        (root / "commands/status.md").write_text("status")
        (root / "apm.yml").write_text("name: normalized\n")
        dependency = LockedDependency(
            repo_url=repo, resolved_commit="a" * 40, package_type="marketplace_plugin"
        )
        dependency.native_plugin = NativePluginRecord(
            name,
            fingerprint_native_claude_source(root),
            *marketplace_binding(self.modules.parent, self.home),
        )
        self.lock.dependencies[repo] = dependency
        return root

    def reconcile(self, previous) -> None:
        reconcile_native_plugins(
            modules_dir=self.modules,
            scope=InstallScope.USER,
            previous=previous,
            lockfile=self.lock,
            deploy_root=self.home,
        )


@pytest.mark.req("req-na-002")
@pytest.mark.skipif(os.name == "nt", reason="the vector pins a POSIX executable bit")
def test_root_digest_matches_the_known_answer_vector(tmp_path: Path) -> None:
    assert_spec_contains(
        "MUST derive the set of\nactivated plugins at a scope only from the lockfile",
        "one root digest covering the path, bytes, and\nexecutable bit of every file in that root",
        "whether or not the consumer generated them",
        "`<profile root>/.claude-plugin/marketplace.json`",
        "MUST\nNOT declare the resolved dependency root to a target as a marketplace",
    )
    assert compute_native_plugin_hash(_known_answer_tree(tmp_path)) == _KNOWN_ANSWER


@pytest.mark.req("req-na-002")
def test_lock_drives_deployment_of_exactly_the_plugin_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _Home(tmp_path, monkeypatch)
    source = home.plugin("owner/alpha", "alpha")
    home.reconcile(None)
    deployed = home.plugins / "alpha"
    record = home.lock.dependencies["owner/alpha"].native_plugin
    assert record.root == ".agents/plugins/alpha"
    assert sorted(
        p.relative_to(deployed).as_posix() for p in deployed.rglob("*") if p.is_file()
    ) == [
        ".claude-plugin/plugin.json",
        "commands/status.md",
    ]
    catalog = json.loads((home.plugins / ".claude-plugin/marketplace.json").read_text())
    assert catalog["plugins"] == [{"name": "alpha", "source": "./alpha"}]
    declared = json.loads(home.settings.read_text())
    paths = [entry["source"]["path"] for entry in declared["extraKnownMarketplaces"].values()]
    assert paths == [str(home.plugins)]
    assert str(home.modules) not in json.dumps(declared)
    (deployed / "commands/status.md").write_text("drifted")
    with pytest.raises(
        AgentPluginDeploymentBoundaryError,
        match=r"alpha.*expected sha256:[0-9a-f]{64}, observed sha256:",
    ):
        verify_claude_native_cache(
            modules_dir=home.modules,
            locked=home.lock.dependencies["owner/alpha"],
            source_root=source,
        )


@pytest.mark.req("req-na-003")
def test_declaration_writes_only_the_two_keys_and_runs_no_vendor_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert_spec_contains(
        "MUST NOT\nwrite any other settings key for this purpose",
        "MUST NOT locate, invoke, or version-check a target-native\nbinary, CLI, or RPC endpoint",
    )
    home = _Home(tmp_path, monkeypatch)
    home.plugin("owner/alpha", "alpha")
    home.settings.parent.mkdir(parents=True)
    home.settings.write_text(json.dumps({"theme": "dark"}))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("native declaration ran a subprocess")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    home.reconcile(None)
    assert set(json.loads(home.settings.read_text())) == {
        "theme",
        "extraKnownMarketplaces",
        "enabledPlugins",
    }


@pytest.mark.req("req-na-004")
@pytest.mark.parametrize(
    "case_id",
    [case["id"] for case in load_json_fixture(*_ORACLE)["input"]["cases"]],
)
def test_ownership_derives_from_previous_and_next_lockfile(case_id: str, tmp_path: Path) -> None:
    """The shared settings writer reproduces every case of the req-na-004 oracle."""
    assert_spec_contains(
        "MUST remove\nonly what the previous lockfile produced with the same binding",
        "MUST NOT change an\nexisting `false` enable value",
        "The\nconsumer MUST NOT create a receipt, ledger, or other ownership file",
    )
    oracle = load_json_fixture(*_ORACLE)
    case = next(c for c in oracle["input"]["cases"] if c["id"] == case_id)
    want = next(c for c in oracle["expected"]["cases"] if c["id"] == case_id)
    marketplace = case["marketplace"]
    previous = case["previous_lock_plugins"]
    moved_from = case.get("previous_marketplace")
    declaration = SettingsDeclaration(
        marketplace["name"],
        marketplace["path"],
        frozenset(case["next_lock_plugins"]),
        None if previous is None else frozenset(previous),
        (moved_from["name"], moved_from["path"]) if moved_from else None,
    )
    if "settings_before_raw" in case:
        settings = tmp_path / "settings.json"
        settings.write_text(case["settings_before_raw"], encoding="utf-8")
        with pytest.raises(NativeSettingsError):
            write_declaration(settings, declaration)
        assert settings.read_text(encoding="utf-8") == want["settings_after_raw"]
        return
    document = copy.deepcopy(case["settings_before"])
    change = apply_declaration(document, declaration, Path("settings.json"))
    assert document in (want.get("settings_after_one_of") or [want["settings_after"]])
    assert change.diagnostics == want["diagnostics"]


@pytest.mark.req("req-na-004")
def test_no_receipt_and_removal_only_of_what_the_previous_lock_produced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _Home(tmp_path, monkeypatch)
    home.plugin("owner/alpha", "alpha")
    home.reconcile(None)
    previous = native_ownership(home.lock)
    stranger = home.plugins / "stranger"
    stranger.mkdir()
    (stranger / "x").write_text("not APM's")
    owned_files = {p.relative_to(home.home).as_posix() for p in home.home.rglob("*") if p.is_file()}
    assert not any("apm-registration" in path or "ledger" in path for path in owned_files)
    del home.lock.dependencies["owner/alpha"]
    home.reconcile(previous)
    assert not (home.plugins / "alpha").exists()
    assert (stranger / "x").read_text() == "not APM's"


@pytest.mark.req("req-na-005")
def test_enable_requires_content_bound_approval(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from apm_cli.claude_plugins.admission import ClaudeNativePlugin, queue_native_claude
    from apm_cli.utils.diagnostics import DiagnosticCollector

    assert_spec_contains(
        "identified by the approval\ndigest of that root rather than by package name",
        "MUST NOT declare it in a disabled state",
    )
    ctx = SimpleNamespace(claude_native_plugins={})
    diagnostics = DiagnosticCollector()
    parked = ClaudeNativePlugin(
        "owner/alpha",
        "alpha",
        tmp_path,
        "sha256:" + "0" * 64,
        parked="Parked native plugin owner/alpha: not approved",
    )
    queue_native_claude(ctx, "owner/alpha", parked, diagnostics)
    assert ctx.claude_native_plugins == {}
    assert any("Parked native plugin" in d.message for d in diagnostics._diagnostics)


@pytest.mark.req("req-na-005")
@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are not enforced on Windows")
def test_approval_digest_binds_paths_and_bytes_but_not_the_executable_bit(tmp_path: Path) -> None:
    """Amendment 0.1.66: one approval covers the same content across operating systems."""
    from apm_cli.security.executables import build_exec_trust_context, materialize_exec_map
    from apm_cli.security.trust import admit_native_plugin
    from apm_cli.security.trust_fingerprint import native_plugin_fingerprint
    from apm_cli.utils.content_hash import compute_native_plugin_hash

    assert_spec_contains(
        "with the executable-bit byte omitted: for each file,",
        "which includes that bit, still gates integrity between the materialized",
    )
    root = tmp_path / "alpha"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin/plugin.json").write_text('{"name": "alpha"}', encoding="utf-8")
    (root / "hooks").mkdir()
    script = root / "hooks/start.sh"
    script.write_text("echo start\n", encoding="utf-8")
    script.chmod(0o644)
    approved = native_plugin_fingerprint(root)
    deployed_digest = compute_native_plugin_hash(root)
    allow = materialize_exec_map(
        build_exec_trust_context(
            policy=None,
            project_data={"executables": {"allow": {"owner/alpha": {"plugin": approved}}}},
        )
    )

    script.chmod(0o755)
    assert native_plugin_fingerprint(root) == approved
    assert admit_native_plugin(allow, ("owner/alpha",), root).allowed
    # The integrity root digest still sees the mode change.
    assert compute_native_plugin_hash(root) != deployed_digest

    script.write_text("echo changed\n", encoding="utf-8")
    assert not admit_native_plugin(allow, ("owner/alpha",), root).allowed


@pytest.mark.req("req-na-006")
def test_unqualified_target_gets_explicit_split_install():
    assert_spec_contains(
        "It MUST NOT split-install a plugin silently",
        "names the target, the scope, and the criterion that is\nunmet or unknown",
        "Split (fails A3",
    )
    waive(
        "The explicit split fallback for Codex, which fails A3, is implemented in "
        "#3920; schema-bearing Agent Plugins v1 dependencies stay opaque under req-tg-011."
    )


@pytest.mark.req("req-na-007")
def test_generated_marketplace_name_is_unique_per_apm_home_and_refuses_rebinding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert_spec_contains(
        "stable across clones of the same project at project scope",
        "different between project scope and user scope",
    )
    assert marketplace_name(tmp_path / "a") == marketplace_name(tmp_path / "a")
    assert marketplace_name(tmp_path / "a") != marketplace_name(tmp_path / "b")
    home = _Home(tmp_path, monkeypatch)
    home.plugin("owner/alpha", "alpha")
    name = marketplace_name(home.modules.parent)
    home.settings.parent.mkdir(parents=True)
    foreign = {"extraKnownMarketplaces": {name: {"source": {"source": "directory", "path": "/x"}}}}
    home.settings.write_text(json.dumps(foreign))
    with pytest.raises(ClaudeNativePluginError, match="another source"):
        home.reconcile(None)
    assert json.loads(home.settings.read_text()) == foreign


@pytest.mark.req("req-na-003")
@pytest.mark.req("req-na-007")
def test_project_scope_declares_a_relative_path_under_a_per_project_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3921: committed project settings name the profile root relatively."""
    from apm_cli.claude_plugins.registrar import scope_binding
    from apm_cli.native_plugins import scopes

    assert_spec_contains(
        "At project scope the\ndeclaration MUST use a relative path",
        "at project\nscope the name MUST NOT depend on the user, the host, or the absolute\ncheckout path",
    )
    monkeypatch.setattr(scopes, "project_identity", lambda root: "example.test/acme/app")
    one = scope_binding(
        user_scope=False,
        modules_dir=tmp_path / "a/apm_modules",
        deploy_root=tmp_path / "a",
        previous=None,
    )
    two = scope_binding(
        user_scope=False,
        modules_dir=tmp_path / "elsewhere/b/apm_modules",
        deploy_root=tmp_path / "elsewhere/b",
        previous=None,
    )
    assert one == two == (one[0], "./.agents/plugins")
    assert one[0] != marketplace_name(tmp_path / "a")
    monkeypatch.setattr(scopes, "project_identity", lambda root: "example.test/acme/other")
    assert (
        scope_binding(
            user_scope=False,
            modules_dir=tmp_path / "a/apm_modules",
            deploy_root=tmp_path / "a",
            previous=None,
        )[0]
        != one[0]
    )


@pytest.mark.req("req-na-002")
@pytest.mark.skipif(os.name == "nt", reason="POSIX exec bit")
def test_project_scope_root_digest_omits_the_exec_bit_and_checks_it_per_machine(
    tmp_path: Path,
) -> None:
    """Amendment 0.1.71: the committed digest is the approval digest construction."""
    from apm_cli.claude_plugins.registrar import _machine_hash
    from apm_cli.security.trust_fingerprint import native_plugin_fingerprint

    assert_spec_contains(
        "At project scope, where the deployed plugin\nroots and the lockfile are project files "
        "shared between machines, the\nrecorded root digest MUST omit the executable-bit byte",
        "MUST NOT record that comparison in\nthe lockfile (amendment 0.1.71)",
    )
    root = tmp_path / "plugin"
    (root / "hooks").mkdir(parents=True)
    script = root / "hooks/run.sh"
    script.write_text("echo hi\n", encoding="utf-8")
    script.chmod(0o755)
    committed = fingerprint_native_claude_source(root, portable=True)
    assert committed == native_plugin_fingerprint(root)
    on_this_machine = _machine_hash(root)
    script.chmod(0o644)
    # A teammate checkout without the bit still matches the committed lock ...
    assert fingerprint_native_claude_source(root, portable=True) == committed
    # ... while the deploying machine still sees the bit.
    assert _machine_hash(root) != on_this_machine


@pytest.mark.req("req-na-004")
def test_cross_target_suppression_is_the_consumers_own_false() -> None:
    """Amendment 0.1.71: APM owns Copilot's key for a plugin it declares to Claude."""
    assert_spec_contains(
        "(cross-target suppression, amendment 0.1.71)",
        "writes a `false` enable value for it in `.github/copilot/settings.json`",
    )
    owned = frozenset({"alpha", "beta"})
    claude_only = SettingsDeclaration(
        "apm-p-1",
        "./.agents/plugins",
        frozenset(),
        owned,
        suppressed=frozenset({"alpha"}),
        managed=frozenset({"alpha"}),
    )
    document = {"enabledPlugins": {"alpha@apm-p-1": True, "beta@apm-p-1": False}}
    apply_declaration(document, claude_only, Path("settings.json"))
    # APM's own true flips to false; a user's false for an unmanaged plugin stays.
    assert document["enabledPlugins"] == {"alpha@apm-p-1": False, "beta@apm-p-1": False}
    both = SettingsDeclaration(
        "apm-p-1",
        "./.agents/plugins",
        frozenset({"alpha"}),
        owned,
        managed=frozenset({"alpha"}),
    )
    apply_declaration(document, both, Path("settings.json"))
    assert document["enabledPlugins"]["alpha@apm-p-1"] is True


def _reconcile(case: dict[str, Any]) -> tuple[Any, list[str]]:
    """Reference model of the req-na-004 rule, used only to check the oracle."""
    if "settings_before_raw" in case:
        return case["settings_before_raw"], ["settings-unparseable"]
    name = case["marketplace"]["name"]
    binding = {"source": {"source": "directory", "path": case["marketplace"]["path"]}}
    before = case["settings_before"]
    moved_from = case.get("previous_marketplace")
    if moved_from is not None and moved_from != case["marketplace"]:
        return copy.deepcopy(before), ["binding-moved"]
    settings = copy.deepcopy(before)
    marketplaces = settings.setdefault("extraKnownMarketplaces", {})
    enabled = settings.setdefault("enabledPlugins", {})
    existing = marketplaces.get(name)
    if existing is not None and existing != binding:
        return copy.deepcopy(before), ["binding-conflict"]
    diagnostics: list[str] = []
    previous = case["previous_lock_plugins"]
    if previous is None and existing is not None:
        diagnostics.append("previous-lock-missing")
    produced = set(previous or ())
    wanted = set(case["next_lock_plugins"])
    for plugin in sorted(produced - wanted):
        if enabled.get(f"{plugin}@{name}") is True:
            del enabled[f"{plugin}@{name}"]
    for plugin in sorted(wanted):
        enabled.setdefault(f"{plugin}@{name}", True)
    if wanted:
        marketplaces[name] = binding
    elif produced and existing is not None:
        del marketplaces[name]
    return settings, diagnostics


def test_req_na_004_oracle_matches_the_reconciliation_rule():
    oracle = load_json_fixture(*_ORACLE)
    assert oracle["spec_anchor"] == "req-na-004"
    expected = {c["id"]: c for c in oracle["expected"]["cases"]}
    inputs = oracle["input"]["cases"]
    assert [c["id"] for c in inputs] == list(expected)
    for case in inputs:
        settings, diagnostics = _reconcile(case)
        want = expected[case["id"]]
        allowed = want.get("settings_after_one_of")
        if allowed is None:
            allowed = [want.get("settings_after", want.get("settings_after_raw"))]
        assert settings in allowed, case["id"]
        assert diagnostics == expected[case["id"]]["diagnostics"], case["id"]
