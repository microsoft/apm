"""Policy: tier selection table and CI parity against the real workflows."""

from __future__ import annotations

from pathlib import Path

import preflight
import preflight_lib as lib
import preflight_policy as policy
import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
CI = REPO / ".github/workflows/ci.yml"
SPEC = REPO / ".github/workflows/spec-conformance.yml"


@pytest.mark.parametrize(
    ("changed", "tiers"),
    [
        (["docs/src/content/docs/guides/x.md"], ["T0"]),
        (["README.md", "CHANGELOG.md"], ["T0"]),
        (["docs/src/content/docs/specs/openapm-v0.1.md"], ["T0", "T1"]),
        (["src/apm_cli/commands/install.py"], ["T0", "T1", "T2", "T3"]),
        (["tests/unit/test_x.py"], ["T0", "T1"]),
        (["tests/integration/test_y.py"], ["T0", "T1", "T3"]),
        (["tests/utils/helpers.py"], ["T0", "T1", "T2", "T3"]),
        (["pyproject.toml"], ["T0", "T1", "T2", "T3"]),
        (["uv.lock"], ["T0", "T2", "T3"]),
        (["scripts/lifecycle_smoke_sharding.py"], ["T0", "T1", "T3"]),
        (["scripts/lint-auth-signals.sh"], ["T0", "T1"]),
        ([".github/workflows/ci.yml"], ["T0", "T1"]),
        (["packages/apm-contributor-dashboard/src/app.mjs"], ["T0", "T1"]),
        (["apm.yml"], ["T0", "T1"]),
        (["apm.lock.yaml"], ["T0", "T1"]),
        ([".apm/skills/x/SKILL.md"], ["T0", "T1"]),
        ([".github/instructions/x.instructions.md"], ["T0", "T1"]),
    ],
)
def test_tier_selection_table(changed: list[str], tiers: list[str]) -> None:
    assert list(policy.select(changed)) == tiers


def test_select_is_ordered_and_explains() -> None:
    selected = policy.select(["src/apm_cli/a.py", "docs/x.md"])
    assert list(selected) == ["T0", "T1", "T2", "T3"]
    assert "src/apm_cli/a.py" in selected["T2"]


def _ctx(changed: list[str], selected: dict[str, str]) -> policy.Context:
    return policy.Context(
        root=REPO,
        base_ref="origin/main",
        base_sha="0" * 40,
        merge_base="0" * 40,
        changed=changed,
        selected=selected,
    )


def _ids(changed: list[str]) -> dict[str, list[str]]:
    selected = policy.select(changed)
    ctx = _ctx(changed, selected)
    return {tier: [c.id for c in policy.build_checks(tier, ctx)] for tier in selected}


def test_t0_contains_every_lint_step_and_guards() -> None:
    ids = _ids(["docs/x.md"])["T0"]
    for expected in (
        "ci.drift",
        "git.merge-conflict",
        "git.diff-check",
        "guard.ascii",
        "lint.ruff",
        "lint.ruff-format",
        "lint.yaml-io",
        "lint.file-length",
        "lint.relpath",
        "lint.pylint-dup",
        "lint.auth-signals",
        "lint.arch-boundaries",
        "spec.mode-b",
    ):
        assert expected in ids
    assert "guard.uv-lock" not in ids
    assert "guard.uv-lock" in _ids(["uv.lock"])["T0"]


def test_src_change_builds_full_lane_set() -> None:
    ids = _ids(["src/apm_cli/core/deployment_ledger.py"])
    assert "unit.mapped" in ids["T1"]
    assert "arch.contracts" in ids["T1"]
    assert "spec.conformance" in ids["T1"]
    assert ids["T2"] == ["unit.full"]
    assert ids["T3"] == ["lifecycle.smoke-1", "lifecycle.smoke-2"]


def test_lane_specific_checks() -> None:
    assert _ids(["packages/apm-contributor-dashboard/x.mjs"])["T1"] == [
        "dashboard.node",
        "apm.self-check",
        "apm.lock-snapshots",
    ]
    assert _ids([".github/workflows/ci.yml"])["T1"] == ["arch.contracts"]
    assert _ids(["apm.yml"])["T1"] == ["apm.self-check", "apm.lock-snapshots"]


def test_package_edits_verify_lockfile_snapshot_hashes() -> None:
    # Regression trap: a packages/ edit with a stale apm.lock.yaml content_hash
    # passed preflight but failed CI's test_local_package_snapshots_match_lockfile.
    ids = _ids(["packages/preflight-gate/.apm/hooks/scripts/preflight_policy.py"])
    assert "apm.lock-snapshots" in ids["T1"]
    assert "test_local_package_snapshots_match_lockfile" in policy.APM_LOCK_SNAPSHOTS


def test_unit_mapping_finds_tests_for_modules_and_scripts() -> None:
    mapped = policy.mapped_unit_tests(REPO, ["src/apm_cli/core/deployment_ledger.py"])
    assert mapped
    assert all(p.startswith(("tests/unit/", "tests/red_team/")) for p in mapped)
    scripts = policy.mapped_unit_tests(REPO, ["scripts/check_test_assertions.py"])
    assert "tests/unit/scripts/test_check_test_assertions.py" in scripts


def test_unit_mapping_cap_defers_to_t2() -> None:
    changed = ["src/apm_cli/__init__.py"]
    selected = policy.select(changed)
    checks = policy.build_checks("T1", _ctx(changed, selected))
    mapped = policy.mapped_unit_tests(REPO, changed)
    if len(mapped) > policy.UNIT_MAP_CAP:
        assert "unit.mapped" not in [c.id for c in checks]


# --- CI parity (R5) -------------------------------------------------------------


def _steps(path: Path, job: str) -> dict[str, str]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {s.get("name"): s.get("run") for s in data["jobs"][job]["steps"] if "name" in s}


def test_lint_commands_are_verbatim_ci() -> None:
    steps = _steps(CI, "lint")
    assert steps["Ruff lint"] == policy.LINT_RUFF
    assert steps["Ruff format check"] == policy.LINT_RUFF_FORMAT
    assert steps["Check YAML encoding safety"] == policy.LINT_YAML_IO
    assert steps["File length guardrail"] == policy.LINT_FILE_LENGTH
    assert steps["Lint - no raw str(relative_to) patterns"] == policy.LINT_RELPATH
    assert steps["Code duplication guardrail (pylint R0801)"] == policy.LINT_PYLINT_DUP
    assert (
        steps["Lint - auth-protocol boundary (#1212 anti-regression)"] == policy.LINT_AUTH_SIGNALS
    )
    assert steps["Lint - architecture authority boundaries"] == policy.LINT_ARCH_BOUNDARIES


def test_other_mirrored_commands_match_ci() -> None:
    assert _steps(CI, "test-architecture-contracts")["Run ratchet contract tests"] == (
        policy.ARCH_CONTRACTS
    )
    assert _steps(CI, "build-and-test-shard")["Run contributor dashboard Node tests"] == (
        policy.DASHBOARD_NODE
    )
    assert _steps(CI, "apm-self-check")["apm audit --ci"] == policy.APM_SELF_CHECK
    spec = _steps(SPEC, "spec-conformance")
    assert spec["Mode B detector (silent-extension gate on critical paths)"] == policy.SPEC_MODE_B
    assert spec["Conformance test suite"] == policy.SPEC_SUITE
    orphan = "Orphan check (anchors == manifest == Appendix C == markers)"
    assert spec[orphan] == policy.SPEC_ORPHAN


def test_lifecycle_smoke_is_ci_minus_nproc_plus_frozen() -> None:
    ci_cmd = _steps(CI, "lifecycle-smoke-shard-1")["Run required lifecycle smoke subset"]
    lines = ci_cmd.splitlines(keepends=True)
    assert lines[:2] == ["printf 'Runner logical CPUs: '\n", "nproc\n"]
    expected = "".join(lines[2:]).replace("uv run --extra dev", "uv run --frozen --extra dev", 1)
    assert expected == policy.LIFECYCLE_SMOKE


def test_unit_suite_covers_ci_shard_targets() -> None:
    ci_cmd = _steps(CI, "build-and-test-shard")["Run tests with coverage"]
    assert "tests/unit tests/test_console.py tests/red_team" in ci_cmd
    assert "--dist worksteal" in ci_cmd
    assert "tests/unit tests/test_console.py tests/red_team" in policy.UNIT_FULL


def test_ci_fingerprints_match_current_workflows() -> None:
    assert lib.ci_fingerprints(REPO, policy.MIRRORED_CI_JOBS) == policy.CI_FINGERPRINTS


def test_ci_drift_check_fails_on_mismatch(workdir: Path) -> None:
    (workdir / ".github/workflows").mkdir(parents=True)
    for path in (CI, SPEC):
        target = workdir / ".github/workflows" / path.name
        target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    ctx = _ctx([], {"T0": "always"})
    ctx.root = workdir
    check = policy.Check("ci.drift", "T0", internal="ci_drift")
    log = workdir / "drift.log"
    assert preflight._run_internal(check, ctx, policy, log) == 0
    ci = workdir / ".github/workflows/ci.yml"
    ci.write_text(
        ci.read_text(encoding="utf-8").replace("MAX_LINES=2100", "MAX_LINES=2200"),
        encoding="utf-8",
    )
    assert preflight._run_internal(check, ctx, policy, log) == 1
    assert "ci.yml:lint" in log.read_text(encoding="utf-8")


def test_job_block_extraction_is_deterministic() -> None:
    text = (
        "name: x\non: push\njobs:\n  a:\n    runs-on: y\n    steps:\n    - run: one\n\n"
        "  # preamble of b\n  b:\n    steps:\n    - run: two\r\nother: 1\n"
    )
    assert lib.ci_job_block(text, "a") == "  a:\n    runs-on: y\n    steps:\n    - run: one\n"
    assert lib.ci_job_block(text, "b") == "  b:\n    steps:\n    - run: two\n"
    assert lib.ci_job_block(text, "missing") is None
    assert lib.ci_job_block("no jobs here", "a") is None
    comment_only = text.replace("# preamble of b", "# edited preamble")
    assert lib.ci_job_block(comment_only, "a") == lib.ci_job_block(text, "a")


def test_policy_source_is_printable_ascii() -> None:
    for name in ("gate.py", "preflight.py", "preflight_lib.py", "preflight_policy.py"):
        text = (Path(policy.__file__).parent / name).read_text(encoding="utf-8")
        assert all(ch in "\n\t" or " " <= ch <= "~" for ch in text), name


def test_provisioning_python_matches_ci() -> None:
    ci = yaml.safe_load(CI.read_text(encoding="utf-8"))
    assert str(ci["env"]["PYTHON_VERSION"]) == policy.CI_PYTHON_VERSION


def test_spec_conformance_skips_public_release_lookup() -> None:
    selected = policy.select(["src/apm_cli/a.py"])
    checks = {c.id: c for c in policy.build_checks("T1", _ctx(["src/apm_cli/a.py"], selected))}
    assert checks["spec.conformance"].env == {"APM_NO_DIRECT_FALLBACK": "1"}
