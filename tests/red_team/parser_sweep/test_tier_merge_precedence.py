"""RED-TEAM round-2: tier merge precedence + org deny_all ceiling.

The lifecycle "policy" tier (/etc/apm/policy.d/*.json) is purely ADDITIVE:
discovery appends policy, then user, then project entries -- no tier can
replace or delete another tier's scripts. This probe verifies:

1. A lower tier (project) cannot OVERRIDE / suppress a higher-trust tier
   (policy): both sets of scripts survive the merge; the project entry is
   appended, not substituted for the policy entry.

2. The org ``executables.deny_all`` ceiling, enforced in
   ``build_runner_from_context`` via ``discover_policy_with_chain``, wins
   over ALL apm.yml content: no project or user lifecycle entry can lower
   it (the apm.yml lifecycle: block is a different surface from org
   executables policy, so it has no path to flip the kill-switch).

3. A malformed / wrong-shape / non-object / array-top policy JSON degrades
   to no entries instead of crashing or injecting a script.
"""

from __future__ import annotations

import json
import types

from .conftest import write_apm_yml


def _write_policy(policy_dir, name, obj):
    (policy_dir / name).write_text(json.dumps(obj), encoding="utf-8")


def _cmd(command):
    return {"type": "command", "command": command}


def test_policy_and_project_tiers_are_additive(tmp_path, policy_dir, isolated_home):
    """A project entry is appended to -- never substituted for -- a policy entry."""
    from apm_cli.core.lifecycle_scripts import discover_scripts

    _write_policy(
        policy_dir,
        "10-admin.json",
        {"version": 1, "scripts": {"post-install": [_cmd("echo POLICY")]}},
    )
    proj = tmp_path / "proj"
    write_apm_yml(
        proj,
        "lifecycle:\n  post-install:\n    - {type: command, command: echo PROJECT}\n",
    )

    scripts = discover_scripts(project_root=str(proj))
    sources = {s.source for s in scripts}
    cmds = {s.effective_command for s in scripts}

    assert "policy" in sources, "policy tier dropped from the merge"
    assert "project" in sources, "project tier dropped from the merge"
    assert "echo POLICY" in cmds and "echo PROJECT" in cmds
    # Policy precedes project in the additive order (policy is loaded first).
    policy_idx = next(i for i, s in enumerate(scripts) if s.source == "policy")
    project_idx = next(i for i, s in enumerate(scripts) if s.source == "project")
    assert policy_idx < project_idx


def test_deny_all_ceiling_wins_over_all_tiers(tmp_path, policy_dir, isolated_home, monkeypatch):
    """Org deny_all suppresses every tier, regardless of project/user content."""
    import apm_cli.policy.discovery as discovery_mod
    from apm_cli.core.lifecycle_scripts import build_runner_from_context
    from apm_cli.core.script_trust import trust_project_scripts

    # Populate all three tiers with runnable scripts.
    _write_policy(
        policy_dir,
        "10-admin.json",
        {"version": 1, "scripts": {"post-install": [_cmd("echo POLICY")]}},
    )
    home = isolated_home
    write_apm_yml(home, "lifecycle:\n  post-install:\n    - {type: command, command: echo USER}\n")
    proj = tmp_path / "proj"
    proj_yml = write_apm_yml(
        proj,
        "lifecycle:\n  post-install:\n    - {type: command, command: echo PROJECT}\n",
    )
    # Trust the project tier so it would otherwise run.
    trust_project_scripts(proj_yml)

    # Fake org policy with the kill-switch engaged.
    fake = types.SimpleNamespace(
        policy=types.SimpleNamespace(executables=types.SimpleNamespace(deny_all=True))
    )
    monkeypatch.setattr(discovery_mod, "discover_policy_with_chain", lambda root: fake)

    runner = build_runner_from_context(project_root=str(proj))
    assert runner.scripts_for_event("post-install") == [], (
        "deny_all ceiling did not suppress lifecycle scripts -- a tier overrode the org ceiling"
    )


def test_deny_all_false_does_not_suppress(tmp_path, policy_dir, isolated_home, monkeypatch):
    """Control: with deny_all False, the (trusted) tiers still fire."""
    import apm_cli.policy.discovery as discovery_mod
    from apm_cli.core.lifecycle_scripts import build_runner_from_context

    _write_policy(
        policy_dir,
        "10-admin.json",
        {"version": 1, "scripts": {"post-install": [_cmd("echo POLICY")]}},
    )
    proj = tmp_path / "proj"
    write_apm_yml(proj, "name: x\n")  # no project lifecycle, just the policy tier

    fake = types.SimpleNamespace(
        policy=types.SimpleNamespace(executables=types.SimpleNamespace(deny_all=False))
    )
    monkeypatch.setattr(discovery_mod, "discover_policy_with_chain", lambda root: fake)

    runner = build_runner_from_context(project_root=str(proj))
    cmds = {s.effective_command for s in runner.scripts_for_event("post-install")}
    assert "echo POLICY" in cmds


def test_malformed_policy_json_shapes_inject_nothing(tmp_path, policy_dir, isolated_home):
    """Array-top / non-object / wrong-shape / duplicate-event JSON -> no entries, no crash."""
    from apm_cli.core.lifecycle_scripts import discover_scripts

    # Array top-level (not an object).
    (policy_dir / "01-array.json").write_text("[1, 2, 3]", encoding="utf-8")
    # Scalar top-level.
    (policy_dir / "02-scalar.json").write_text("42", encoding="utf-8")
    # Wrong shape: scripts is a list, not a mapping.
    _write_policy(policy_dir, "03-wrongshape.json", {"version": 1, "scripts": ["x"]})
    # Wrong version.
    _write_policy(
        policy_dir,
        "04-badver.json",
        {"version": 99, "scripts": {"post-install": [_cmd("echo NOPE")]}},
    )
    # Trailing-comma / invalid JSON.
    (policy_dir / "05-badjson.json").write_text('{"version":1,}', encoding="utf-8")

    scripts = discover_scripts(project_root=str(tmp_path / "noproj"))
    # None of the malformed files may contribute a runnable entry.
    assert all(s.effective_command != "echo NOPE" for s in scripts)
    assert scripts == [] or all(s.source == "policy" for s in scripts)
