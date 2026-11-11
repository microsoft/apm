"""Runner behavior with a fake policy: receipts, eligibility, hygiene, guards."""

from __future__ import annotations

import json
from pathlib import Path

import preflight
import preflight_lib as lib
import preflight_policy as policy
import pytest
from conftest import Sandbox


def run(box: Sandbox, fake_policy, *argv: str) -> int:
    return preflight.run(list(argv), policy=fake_policy, cwd=box.root)


def test_clean_pass_writes_commit_bound_receipt(sandbox: Sandbox, fake_policy) -> None:
    sandbox.write("src/a.py", "x = 1\n")
    head = sandbox.commit()
    assert run(sandbox, fake_policy) == 0
    receipt = sandbox.receipt()
    assert receipt["schema"] == 1
    assert receipt["status"] == "pass"
    assert receipt["gate_eligible"] is True
    assert receipt["head"] == head
    assert receipt["tree"] == sandbox.git("rev-parse", "HEAD^{tree}")
    assert receipt["base_ref"] == "origin/main"
    assert receipt["base_branch"] == "main"
    assert receipt["base_sha"] == sandbox.git("rev-parse", "origin/main")
    assert receipt["required_tiers"] == {"T0": "always", "T1": "src changed"}
    assert receipt["ran_tiers"] == ["T0", "T1"]
    assert receipt["bundle_hash"] == lib.deployed_bundle_hash(sandbox.root)
    assert receipt["ci_only_lanes"] == ["Windows (CI only)"]
    assert {c["id"] for c in receipt["checks"]} == {"t0.ok", "t0.flag", "t1.ok", "t1.mutate"}
    for check in receipt["checks"]:
        assert Path(check["log"]).is_file()
        assert Path(check["log"]).parent == lib.preflight_dir(sandbox.git_dir) / "logs"


def test_receipt_lives_in_git_dir_not_worktree(sandbox: Sandbox, fake_policy) -> None:
    assert run(sandbox, fake_policy) == 0
    assert lib.receipt_path(sandbox.git_dir).is_file()
    assert lib.is_clean(sandbox.root)


def test_dirty_worktree_runs_but_is_not_eligible(sandbox: Sandbox, fake_policy) -> None:
    sandbox.write("src/dirty.py", "x = 1\n")
    assert run(sandbox, fake_policy) == 1
    receipt = sandbox.receipt()
    assert receipt["status"] == "pass"
    assert receipt["gate_eligible"] is False
    assert any("dirty" in r for r in receipt["ineligible_reasons"])
    assert receipt["required_tiers"]["T1"] == "src changed"


def test_explicit_tiers_subset_is_partial_and_ineligible(sandbox: Sandbox, fake_policy) -> None:
    sandbox.write("src/a.py", "x = 1\n")
    sandbox.commit()
    assert run(sandbox, fake_policy, "--tiers", "T0") == 1
    receipt = sandbox.receipt()
    assert receipt["status"] == "partial"
    assert receipt["gate_eligible"] is False
    assert run(sandbox, fake_policy, "--tiers", "T1,T0") == 1
    receipt = sandbox.receipt()
    assert receipt["ran_tiers"] == ["T0", "T1"]
    assert receipt["status"] == "pass"
    assert receipt["gate_eligible"] is False
    assert "--tiers was not auto" in receipt["ineligible_reasons"]


def test_bad_tiers_value_is_usage_error(sandbox: Sandbox, fake_policy) -> None:
    with pytest.raises(preflight.UsageError):
        run(sandbox, fake_policy, "--tiers", "T9")


def test_failure_still_runs_shared_checks_of_every_tier(sandbox: Sandbox, fake_policy) -> None:
    sandbox.write("FAIL_T0", "1\n")
    sandbox.write("src/a.py", "x = 1\n")
    sandbox.commit()
    assert run(sandbox, fake_policy) == 1
    receipt = sandbox.receipt()
    assert receipt["status"] == "fail"
    assert receipt["ran_tiers"] == ["T0", "T1"]
    assert receipt["skipped_checks"] == []
    assert [c["id"] for c in receipt["checks"] if c["exit"]] == ["t0.flag"]


def test_tree_mutation_is_detected(sandbox: Sandbox, fake_policy) -> None:
    sandbox.write("src/a.py", "x = 1\n")
    sandbox.write("MUTATE", "src/a.py\n")
    sandbox.commit()
    assert run(sandbox, fake_policy) == 1
    receipt = sandbox.receipt()
    ids = [c["id"] for c in receipt["checks"] if c["exit"]]
    assert "hygiene.tree-mutated" in ids
    assert receipt["status"] == "fail"


def test_plan_writes_no_receipt(sandbox: Sandbox, fake_policy, capsys) -> None:
    sandbox.write("src/a.py", "x = 1\n")
    sandbox.commit()
    assert run(sandbox, fake_policy, "--plan", "--json") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["required_tiers"] == {"T0": "always", "T1": "src changed"}
    assert out["planned"]["T1"] == ["t1.ok", "t1.mutate"]
    assert not lib.receipt_path(sandbox.git_dir).exists()


def test_json_flag_prints_receipt(sandbox: Sandbox, fake_policy, capsys) -> None:
    assert run(sandbox, fake_policy, "--json") == 0
    assert json.loads(capsys.readouterr().out)["status"] == "pass"


def test_missing_base_falls_back_to_main(sandbox: Sandbox, fake_policy) -> None:
    assert run(sandbox, fake_policy, "--base", "upstream/nope", "--no-fetch") == 0
    receipt = sandbox.receipt()
    assert receipt["base_ref"] == "main"
    assert any("fell back" in w for w in receipt["warnings"])


def test_windows_is_refused(sandbox: Sandbox, fake_policy, monkeypatch) -> None:
    monkeypatch.setattr(preflight, "_is_windows", lambda: True)
    assert run(sandbox, fake_policy) == 2


def test_main_maps_usage_errors_to_exit_2(sandbox: Sandbox, monkeypatch) -> None:
    monkeypatch.chdir(sandbox.root)
    assert preflight.main(["--tiers", "T9", "--no-fetch"]) == 2


def test_worktree_tree_equals_head_tree_after_commit(sandbox: Sandbox) -> None:
    sandbox.write("src/new.py", "x = 1\n")
    sandbox.write("README.md", "changed\n")
    dirty_tree = lib.worktree_tree(sandbox.root, sandbox.git_dir)
    assert sandbox.git("status", "--porcelain") != ""
    sandbox.commit()
    assert dirty_tree == sandbox.git("rev-parse", "HEAD^{tree}")
    assert lib.worktree_tree(sandbox.root, sandbox.git_dir) == dirty_tree


def test_changed_files_include_untracked(sandbox: Sandbox) -> None:
    sandbox.write("src/committed.py", "x = 1\n")
    sandbox.commit()
    sandbox.write("src/untracked.py", "y = 2\n")
    base = sandbox.git("merge-base", "origin/main", "HEAD")
    assert lib.changed_files(sandbox.root, base) == ["src/committed.py", "src/untracked.py"]


# --- internal checks -----------------------------------------------------------


def _ctx(box: Sandbox, changed: list[str]) -> policy.Context:
    base = box.git("merge-base", "origin/main", "HEAD")
    return policy.Context(
        root=box.root,
        base_ref="origin/main",
        base_sha=box.git("rev-parse", "origin/main"),
        merge_base=base,
        changed=changed,
        selected={"T0": "always"},
    )


def test_ascii_guard_flags_added_lines_only(sandbox: Sandbox) -> None:
    base = sandbox.git("rev-parse", "HEAD")
    sandbox.write("src/ok.py", "x = 'plain ascii'\n")
    sandbox.write("docs/free.md", "caf\u00e9 is fine in docs\n")
    ok, problems = lib.ascii_guard(sandbox.root, base, policy.ASCII_GUARD_PATHS)
    assert ok, problems
    sandbox.write("src/bad.py", "x = '\u2014'\n")
    sandbox.commit()
    ok, problems = lib.ascii_guard(sandbox.root, base, policy.ASCII_GUARD_PATHS)
    assert not ok
    assert problems[0].startswith("src/bad.py: non-ASCII U+2014")


def _uvlock(revision: int, url: str) -> str:
    return (
        f"version = 1\nrevision = {revision}\n\n[[package]]\n"
        f'name = "x"\nsource = {{ registry = "{url}" }}\n'
    )


def test_uvlock_guard(sandbox: Sandbox) -> None:
    sandbox.write("uv.lock", _uvlock(3, "https://pypi.org/simple"))
    base = sandbox.commit("lock")
    sandbox.write("uv.lock", _uvlock(3, "https://files.pythonhosted.org/packages/a.whl"))
    assert lib.uvlock_guard(sandbox.root, base, policy.UVLOCK_ALLOWED_HOSTS) == (True, [])
    sandbox.write("uv.lock", _uvlock(3, "https://evil.example.com/simple"))
    ok, problems = lib.uvlock_guard(sandbox.root, base, policy.UVLOCK_ALLOWED_HOSTS)
    assert not ok
    assert "'evil.example.com'" in problems[0]
    sandbox.write("uv.lock", _uvlock(2, "https://pypi.org/simple"))
    ok, problems = lib.uvlock_guard(sandbox.root, base, policy.UVLOCK_ALLOWED_HOSTS)
    assert not ok
    assert "revision decreased 3 -> 2" in problems[0]


def test_bundle_stale_internal_check(sandbox: Sandbox, workdir: Path) -> None:
    source = sandbox.root / lib.SOURCE_SCRIPTS_REL
    source.mkdir(parents=True)
    (source / "marker.txt").write_text("bundle v1\n", encoding="utf-8")
    sandbox.commit()
    ctx = _ctx(sandbox, [])
    check = policy.Check("bundle.stale", "T0", internal="bundle_stale")
    log = workdir / "b.log"
    assert preflight._run_internal(check, ctx, policy, log) == 0
    (source / "marker.txt").write_text("bundle v2\n", encoding="utf-8")
    assert preflight._run_internal(check, ctx, policy, log) == 1
    assert "content differs: marker.txt" in log.read_text(encoding="utf-8")
    assert policy.build_checks("T0", ctx)[0].id == "bundle.stale"


def test_bundle_hash_ignores_bytecode(workdir: Path) -> None:
    scripts = workdir / "scripts"
    (scripts / "__pycache__").mkdir(parents=True)
    (scripts / "gate.py").write_text("x = 1\n", encoding="utf-8")
    hook = workdir / "hook.json"
    hook.write_text("{}\n", encoding="utf-8")
    before = lib.bundle_hash(scripts, hook)
    (scripts / "__pycache__" / "gate.cpython-312.pyc").write_bytes(b"\0")
    (scripts / "x.pyc").write_bytes(b"\0")
    assert lib.bundle_hash(scripts, hook) == before
    hook.write_text('{"v": 2}\n', encoding="utf-8")
    assert lib.bundle_hash(scripts, hook) != before


def test_pcre_prelude_selection(monkeypatch) -> None:
    checks = [policy.Check("lint.yaml-io", "T0", cmd="x", needs_pcre_grep=True)]
    monkeypatch.setattr(lib, "host_has_pcre_grep", lambda: True)
    assert preflight.shell_prelude(checks, []) == ("", None)
    monkeypatch.setattr(lib, "host_has_pcre_grep", lambda: False)
    monkeypatch.setattr(preflight.shutil, "which", lambda _name: "/opt/ggrep")
    warnings: list[str] = []
    assert preflight.shell_prelude(checks, warnings) == (preflight.GGREP_PRELUDE, None)
    assert warnings
    monkeypatch.setattr(preflight.shutil, "which", lambda _name: None)
    prelude, problem = preflight.shell_prelude(checks, [])
    assert prelude == ""
    assert "brew install grep" in problem


# --- volatile outputs, CI-like env, lock sync (D3) ------------------------------


def _with_t0(fake_policy, *cmds: str):
    from types import SimpleNamespace

    def build(tier: str, _ctx) -> list[policy.Check]:
        if tier != "T0":
            return []
        return [policy.Check(f"c{i}", "T0", cmd=c) for i, c in enumerate(cmds)]

    return SimpleNamespace(**{**vars(fake_policy), "build_checks": build})


def test_volatile_outputs_are_restored_and_recorded(sandbox: Sandbox, fake_policy) -> None:
    sandbox.write("CONFORMANCE.json", '{"v": 1}\n')
    sandbox.commit()
    pol = _with_t0(
        fake_policy,
        "echo regenerated > CONFORMANCE.json",
        "mkdir -p .github && echo '{}' > .github/mcp.json",
    )
    assert run(sandbox, pol, "--no-fetch") == 0
    receipt = sandbox.receipt()
    assert receipt["status"] == "pass"
    assert receipt["gate_eligible"] is True
    assert receipt["restored_outputs"] == ["CONFORMANCE.json", ".github/mcp.json"]
    assert (sandbox.root / "CONFORMANCE.json").read_text(encoding="utf-8") == '{"v": 1}\n'
    assert not (sandbox.root / ".github/mcp.json").exists()
    assert lib.is_clean(sandbox.root)


def test_other_mutations_still_fail(sandbox: Sandbox, fake_policy) -> None:
    sandbox.write("CONFORMANCE.json", '{"v": 1}\n')
    sandbox.commit()
    pol = _with_t0(fake_policy, "echo x > CONFORMANCE.json", "echo x >> README.md")
    assert run(sandbox, pol, "--no-fetch") == 1
    receipt = sandbox.receipt()
    assert receipt["restored_outputs"] == ["CONFORMANCE.json"]
    assert "hygiene.tree-mutated" in [c["id"] for c in receipt["checks"] if c["exit"]]


def test_command_checks_get_a_ci_like_env(
    sandbox: Sandbox, fake_policy, workdir: Path, monkeypatch
) -> None:
    monkeypatch.setenv("APM_SECRET_SETTING", "leak")
    monkeypatch.setenv("GITHUB_TOKEN", "leak")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "leak")
    monkeypatch.setenv("UV_NO_SYNC", "1")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'credential.helper=leak'")
    dump = workdir / "env.txt"
    pol = _with_t0(fake_policy, f"env > {dump}")
    assert run(sandbox, pol, "--no-fetch") == 0
    env = dict(line.split("=", 1) for line in dump.read_text().splitlines() if "=" in line)
    assert "APM_SECRET_SETTING" not in env
    assert "GITHUB_TOKEN" not in env
    assert "CLAUDE_CONFIG_DIR" not in env
    assert "GIT_CONFIG_PARAMETERS" not in env
    assert env["UV_NO_SYNC"] == "1"
    assert env["HOME"] == str(lib.preflight_dir(sandbox.git_dir) / "home")
    assert env["USER"] == env["LOGNAME"] == "runner"
    assert env["XDG_CONFIG_HOME"].startswith(env["HOME"])
    assert env["TMPDIR"].endswith("/apm-pf")
    assert len(env["TMPDIR"]) < 60
    assert env["UV_CACHE_DIR"].endswith("/uv")
    assert env["PREFLIGHT_BASE_REF"] == "origin/main"


def test_ci_like_env_keeps_tool_locations(workdir: Path) -> None:
    env = lib.ci_like_env(
        {"PATH": "/bin", "GIT_EXEC_PATH": "/opt/git/libexec", "HOME": str(workdir)},
        workdir / "home",
        workdir / "tmp",
    )
    assert env["GIT_EXEC_PATH"] == "/opt/git/libexec"
    assert env["PATH"] == "/bin"


def test_lock_sync_is_selected_only_with_test_tiers(sandbox: Sandbox) -> None:
    ctx = _ctx(sandbox, ["docs/x.md"])
    assert "env.lock-sync" not in [c.id for c in policy.build_checks("T0", ctx)]
    ctx.selected = {"T0": "", "T1": ""}
    assert "env.lock-sync" in [c.id for c in policy.build_checks("T0", ctx)]


def test_isolated_home_with_locked_dirs_is_reset(sandbox: Sandbox, fake_policy) -> None:
    locked = lib.preflight_dir(sandbox.git_dir) / "home" / ".cache" / "apm" / "x" / "y"
    locked.mkdir(parents=True)
    (locked / "f").write_text("x", encoding="utf-8")
    locked.chmod(0o200)
    locked.parent.chmod(0o200)
    assert run(sandbox, fake_policy, "--no-fetch") == 0
    assert not (lib.preflight_dir(sandbox.git_dir) / "home" / ".cache" / "apm").exists()
