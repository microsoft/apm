"""Preflight policy for microsoft/apm: the single owner of WHAT runs.

Every command below mirrors a ci.yml (or spec-conformance.yml) step. The
fingerprint table binds this mirror to the exact workflow text: when a
mirrored job changes, the `ci.drift` check FAILS until this file is updated
(`python3 preflight.py --print-ci-fingerprints` prints the new values).

Local deviations from CI (all documented in the package README):
- unit suite runs both pytest-split shards as one invocation with
  `-n auto` and no coverage floors (coverage stays CI-only);
- lifecycle smoke drops the cosmetic `printf ...; nproc` preamble (no
  `nproc` on macOS) and adds `--frozen` so a local run cannot rewrite
  uv.lock;
- ratchet contracts run with APM_ALLOW_PROVISIONAL_BASELINES=0 (the strict,
  non-draft PR value).
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

TIERS = ("T0", "T1", "T2", "T3")

UV = "uv run --frozen --extra dev"

# key -> (workflow path, job key). Fingerprints: see preflight_lib.ci_job_block.
MIRRORED_CI_JOBS: dict[str, tuple[str, str]] = {
    "ci.yml:lint": (".github/workflows/ci.yml", "lint"),
    "ci.yml:test-architecture-contracts": (
        ".github/workflows/ci.yml",
        "test-architecture-contracts",
    ),
    "ci.yml:build-and-test-shard": (".github/workflows/ci.yml", "build-and-test-shard"),
    "ci.yml:apm-self-check": (".github/workflows/ci.yml", "apm-self-check"),
    "ci.yml:lifecycle-smoke-shard-1": (".github/workflows/ci.yml", "lifecycle-smoke-shard-1"),
    "ci.yml:lifecycle-smoke-shard-2": (".github/workflows/ci.yml", "lifecycle-smoke-shard-2"),
    "spec-conformance.yml:spec-conformance": (
        ".github/workflows/spec-conformance.yml",
        "spec-conformance",
    ),
}

CI_FINGERPRINTS: dict[str, str] = {
    "ci.yml:lint": "877ba552432268c74dae1e8763900a0a2cfb19bafe6971bd2fc0b631d345b133",
    "ci.yml:test-architecture-contracts": "b242c6a03d7dbb56a1a3cc1932fa8431ebe8dbd7d75401417d045a7609c2f62b",
    "ci.yml:build-and-test-shard": "5bcbeefcc68988f4624189cb087175b29f395698e22900649e94f39ce9ef37e5",
    "ci.yml:apm-self-check": "8830b9010c5de649e63c20f89b77d1994ea8778617db5a883613218f97308706",
    "ci.yml:lifecycle-smoke-shard-1": "31dfab3f9373b96c46b4941fe9d8d6cbd7dbeb6bb97defc08ffe44519a481ae9",
    "ci.yml:lifecycle-smoke-shard-2": "f6b95c6a774e37ab6159c271ce6e92ebbd1185284a6691c269cff03675e7368f",
    "spec-conformance.yml:spec-conformance": "920860630499bab79c30540c3f1327b74faea3a7917ec4556a2186f7a353e358",
}

# Lanes not reproducible locally; they still run in CI. Recorded in every receipt.
CI_ONLY_LANES = (
    "Windows Compatibility Gate (windows-latest)",
    "Windows native standard-user symlink gate",
    "Windows git discovery",
    "Architecture Scaling (perf benchmark, host-sensitive)",
    "Coverage floors (per-shard 60%, combined 80%)",
    "PR Binary Smoke (PyInstaller build)",
    "Spec conformance statement regeneration + clean-diff gate",
    "Merge-queue integration suites (ci-integration.yml)",
    "Release validation and macOS/Windows matrix (build-release.yml)",
)

ASCII_GUARD_PATHS = ["src/", "scripts/", ".github/", ".apm/", "packages/", "CHANGELOG.md"]
UVLOCK_ALLOWED_HOSTS = ("pypi.org", "files.pythonhosted.org")
UNIT_MAP_CAP = 150
UNIT_MAP_XDIST_THRESHOLD = 20

# Files checks are known to (re)write. After the run the runner restores each
# to its pre-run bytes (or deletes it if it did not exist) and records it in
# the receipt as `restored_outputs`; they are excluded from tree-mutation.
#  - CONFORMANCE.json/.md: tests/spec_conformance/test_gen_statement.py
#    regenerates them in place (CI regenerates and diff-gates them separately).
#  - .github/mcp.json: observed after the unit suite (test pollution of cwd).
VOLATILE_OUTPUTS = ("CONFORMANCE.json", "CONFORMANCE.md", ".github/mcp.json")

# --- verbatim ci.yml `lint` job steps --------------------------------------

LINT_RUFF = (
    "uv run --frozen --extra dev ruff check"
    " src/ tests/"
    " scripts/lint_architecture_boundaries.py"
    " scripts/architecture_linter/"
    " scripts/windows_native_symlink_probe_entry.py"
)

LINT_RUFF_FORMAT = (
    "uv run --frozen --extra dev ruff format --check"
    " src/ tests/"
    " scripts/lint_architecture_boundaries.py"
    " scripts/architecture_linter/"
    " scripts/windows_native_symlink_probe_entry.py"
)

LINT_YAML_IO = """\
# Ensure YAML file I/O goes through yaml_io helpers.
# Catches yaml.dump/safe_dump writing to a file handle outside yaml_io.py.
VIOLATIONS=$(grep -rn --include='*.py' -P \\
  'yaml\\.(safe_)?dump\\(.+,\\s*[a-zA-Z_]\\w*\\b' src/apm_cli/ \\
  | grep -v 'utils/yaml_io.py' \\
  | grep -v '# yaml-io-exempt' \\
  || true)
if [ -n "$VIOLATIONS" ]; then
  echo "::error::Direct yaml.dump() to file handle detected. Use yaml_io.dump_yaml() instead:"
  echo "$VIOLATIONS"
  exit 1
fi
"""

LINT_FILE_LENGTH = """\
# Ruff has no max-module-lines rule. This check prevents new files from
# exceeding the current worst case. Tighten the threshold over time.
MAX_LINES=2100   # Stage 1 (was 2450); target 1400 deferred to Stage 2
VIOLATIONS=$(
  {
    find src/ scripts/architecture_linter/ -name '*.py' -print0
    printf '%s\\0' scripts/lint_architecture_boundaries.py
  } | xargs -0 -I{} awk -v max="$MAX_LINES" \\
    'END { if (NR > max) printf "%s: %d lines (max %d)\\n", FILENAME, NR, max }' {}
)
if [ -n "$VIOLATIONS" ]; then
  echo "::error::Source files exceed $MAX_LINES-line limit:"
  echo "$VIOLATIONS"
  exit 1
fi
"""

LINT_RELPATH = """\
# Fail if any code uses str(x.relative_to(y)) instead of portable_relpath()
if grep -rn --include="*.py" -P 'str\\([^)]*\\.relative_to\\(' src/apm_cli/ | grep -v portable_relpath | grep -v '\\.pyc'; then
  echo "::error::Found raw str(path.relative_to()) calls. Use portable_relpath() from apm_cli.utils.paths instead."
  exit 1
fi
"""

LINT_PYLINT_DUP = """\
uv run --frozen --extra dev python -m pylint \\
  --disable=all --enable=R0801 \\
  --min-similarity-lines=10 \\
  --fail-on=R0801 \\
  src/apm_cli/ \\
  scripts/lint_architecture_boundaries.py \\
  scripts/architecture_linter/ \\
  scripts/windows_native_symlink_probe_entry.py
"""

LINT_AUTH_SIGNALS = "bash scripts/lint-auth-signals.sh"
LINT_ARCH_BOUNDARIES = "bash scripts/lint-architecture-boundaries.sh"

# --- other mirrored jobs -----------------------------------------------------

ARCH_CONTRACTS = """\
uv run --frozen --extra dev pytest -p no:cacheprovider -q \\
  tests/unit/scripts/test_check_test_assertions.py \\
  tests/unit/scripts/test_check_exact_test_duplicates.py \\
  tests/quality/test_test_taxonomy.py \\
  tests/quality/test_quality_baselines.py \\
  tests/quality/test_lifecycle_bug_ledger.py \\
  tests/quality/test_ci_topology.py
"""

DASHBOARD_NODE = (
    "node --test"
    " packages/apm-contributor-dashboard/tests/logic.test.mjs"
    " packages/apm-contributor-dashboard/tests/server.test.mjs"
)

APM_SELF_CHECK = "uv run --frozen apm audit --ci"

SPEC_ORPHAN = "uv run --frozen --extra dev python -m tests.spec_conformance.orphan_check"
SPEC_MODE_B = "bash tests/spec_conformance/mode_b_detector.sh"
SPEC_SUITE = """\
uv run --frozen --extra dev pytest tests/spec_conformance \\
  -p no:randomly \\
  --no-header \\
  -q
"""

# CI: `uv run pytest tests/unit tests/test_console.py tests/red_team
#   --splits 2 --group N -n 2 --dist worksteal --cov ...` (two shards).
UNIT_FULL = f"{UV} pytest tests/unit tests/test_console.py tests/red_team -n auto --dist worksteal"

LIFECYCLE_SMOKE = """\
uv run --frozen --extra dev python -m pytest -p no:cacheprovider -q --strict-markers \\
  -p scripts.lifecycle_smoke_sharding --lifecycle-shard "$LIFECYCLE_SHARD" \\
  -n 4 --dist loadgroup --durations=20 \\
  -m 'lifecycle_smoke and not lifecycle_merge_group' tests/integration
"""

GIT_MERGE_PROBE = 'git merge-tree --write-tree "$PREFLIGHT_BASE_SHA" HEAD'
GIT_DIFF_CHECK = 'git diff --check "$PREFLIGHT_MERGE_BASE"'

# --- path rules ----------------------------------------------------------------

SPEC_TRIGGER_PREFIXES = (
    "docs/src/content/docs/specs/",
    "tests/fixtures/spec-conformance/",
    "tests/spec_conformance/",
    "src/apm_cli/",
)
SPEC_TRIGGER_FILES = (
    "pyproject.toml",
    "CONFORMANCE.md",
    "CONFORMANCE.json",
    "CONTRIBUTING.md",
    ".github/workflows/spec-conformance.yml",
)
T2_PREFIXES = ("src/", "tests/utils/")
T2_FILES = ("pyproject.toml", "uv.lock", "tests/conftest.py", "tests/unit/conftest.py")
T3_PREFIXES = ("src/", "tests/integration/", "tests/utils/")
T3_FILES = (
    "pyproject.toml",
    "uv.lock",
    "tests/conftest.py",
    "scripts/lifecycle_smoke_sharding.py",
)
DASHBOARD_PREFIX = "packages/apm-contributor-dashboard/"
SELF_CHECK_FILES = ("apm.yml", "apm.lock.yaml")
SELF_CHECK_PREFIXES = ("packages/", ".apm/", ".agents/", ".github/")
UNIT_TEST_PREFIXES = ("tests/unit/", "tests/red_team/")
UNIT_SEARCH_ROOTS = ("tests/unit", "tests/red_team", "tests/test_console.py")


@dataclass
class Check:
    """One unit of verification. Exactly one of `cmd` / `internal` is set.

    Non-exclusive checks of all selected tiers run concurrently (phase A);
    `exclusive` checks (xdist / E2E suites that saturate every core) run
    afterwards, one at a time (phase B).
    """

    id: str
    tier: str
    cmd: str | None = None
    internal: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    needs_pcre_grep: bool = False
    exclusive: bool = False


@dataclass
class Context:
    """Inputs the runner hands to build_checks()."""

    root: Path
    base_ref: str
    base_sha: str
    merge_base: str
    changed: list[str]
    selected: dict[str, str]


def _starts(path: str, prefixes: tuple[str, ...]) -> bool:
    return path.startswith(prefixes)


def _is_workflow(path: str) -> bool:
    return path.startswith(".github/workflows/")


def _is_self_check(path: str) -> bool:
    if path in SELF_CHECK_FILES:
        return True
    return _starts(path, SELF_CHECK_PREFIXES) and not _is_workflow(path)


def _is_spec(path: str) -> bool:
    return path in SPEC_TRIGGER_FILES or _starts(path, SPEC_TRIGGER_PREFIXES)


def _needs_arch_contracts(path: str) -> bool:
    return (
        path.endswith(".py") or _starts(path, ("src/", "tests/", "scripts/")) or _is_workflow(path)
    )


def _is_unit_test_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    is_test = name.startswith("test_") and name.endswith(".py")
    return is_test and (path == "tests/test_console.py" or _starts(path, UNIT_TEST_PREFIXES))


def _t1_rules() -> list[tuple[str, object]]:
    return [
        ("src/** changed", lambda p: p.startswith("src/")),
        ("tests/** changed", lambda p: p.startswith("tests/")),
        ("scripts/** changed", lambda p: p.startswith("scripts/")),
        (".github/workflows/** changed (CI topology)", _is_workflow),
        ("contributor dashboard changed", lambda p: p.startswith(DASHBOARD_PREFIX)),
        ("APM-managed content changed (self-check)", _is_self_check),
        ("spec conformance paths changed", _is_spec),
        ("python file changed (repo inventory ratchets)", lambda p: p.endswith(".py")),
    ]


def _reason(label: str, hits: list[str]) -> str:
    sample = ", ".join(hits[:3])
    more = f" (+{len(hits) - 3} more)" if len(hits) > 3 else ""
    return f"{label}: {sample}{more}"


def select(changed: list[str]) -> dict[str, str]:
    """Pure: map changed paths to ordered {tier: reason}. T0 is always on."""
    tiers: dict[str, str] = {"T0": "always (lint, hygiene, CI parity)"}
    t1 = []
    for label, pred in _t1_rules():
        hits = [p for p in changed if pred(p)]  # type: ignore[operator]
        if hits:
            t1.append(_reason(label, hits))
    if t1:
        tiers["T1"] = "; ".join(t1)
    t2 = [p for p in changed if p in T2_FILES or _starts(p, T2_PREFIXES)]
    if t2:
        tiers["T2"] = _reason("full unit suite inputs changed", t2)
    t3 = [p for p in changed if p in T3_FILES or _starts(p, T3_PREFIXES)]
    if t3:
        tiers["T3"] = _reason("lifecycle smoke inputs changed", t3)
    return tiers


def _module_patterns(src_path: str) -> list[re.Pattern[str]]:
    """Regexes that indicate a test references the module at *src_path*."""
    rel = src_path[len("src/") :]
    if not rel.endswith(".py"):
        return []
    parts = rel[: -len(".py")].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return []
    dotted = ".".join(parts)
    patterns = [re.compile(re.escape(dotted) + r"\b")]
    if len(parts) > 1:
        parent = re.escape(".".join(parts[:-1]))
        leaf = re.escape(parts[-1])
        patterns.append(re.compile(rf"from {parent} import \(?[^)]*?\b{leaf}\b", re.DOTALL))
    return patterns


def _script_patterns(script_path: str) -> list[re.Pattern[str]]:
    stem = script_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if not stem:
        return []
    return [re.compile(re.escape(stem))]


def _unit_test_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for rel in UNIT_SEARCH_ROOTS:
        base = root / rel
        if base.is_file():
            files.append(base)
        elif base.is_dir():
            files.extend(sorted(base.rglob("test_*.py")))
    return files


def mapped_unit_tests(root: Path, changed: list[str]) -> list[str]:
    """Changed unit test files plus tests that reference changed modules."""
    selected = {p for p in changed if _is_unit_test_file(p) and (root / p).is_file()}
    patterns: list[re.Pattern[str]] = []
    for path in changed:
        if path.startswith("src/"):
            patterns.extend(_module_patterns(path))
        elif path.startswith("scripts/"):
            patterns.extend(_script_patterns(path))
    if patterns:
        for test in _unit_test_files(root):
            try:
                text = test.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if any(p.search(text) for p in patterns):
                selected.add(test.relative_to(root).as_posix())
    return sorted(selected)


def _quote_all(paths: list[str]) -> str:
    return " ".join(shlex.quote(p) for p in paths)


def _t0_checks(ctx: Context) -> list[Check]:
    checks = []
    if (ctx.root / "packages/preflight-gate/.apm/hooks/scripts").is_dir():
        checks.append(Check("bundle.stale", "T0", internal="bundle_stale"))
    if any(t in ctx.selected for t in ("T1", "T2", "T3")):
        # CI runs every test lane after `uv sync --frozen --extra dev`.
        checks.append(Check("env.lock-sync", "T0", internal="lock_sync"))
    checks += [
        Check("ci.drift", "T0", internal="ci_drift"),
        Check("git.merge-conflict", "T0", cmd=GIT_MERGE_PROBE),
        Check("git.diff-check", "T0", cmd=GIT_DIFF_CHECK),
        Check("guard.ascii", "T0", internal="ascii_guard"),
    ]
    if "uv.lock" in ctx.changed:
        checks.append(Check("guard.uv-lock", "T0", internal="uvlock_guard"))
    checks += [
        Check("lint.ruff", "T0", cmd=LINT_RUFF),
        Check("lint.ruff-format", "T0", cmd=LINT_RUFF_FORMAT),
        Check("lint.yaml-io", "T0", cmd=LINT_YAML_IO, needs_pcre_grep=True),
        Check("lint.file-length", "T0", cmd=LINT_FILE_LENGTH),
        Check("lint.relpath", "T0", cmd=LINT_RELPATH, needs_pcre_grep=True),
        Check("lint.pylint-dup", "T0", cmd=LINT_PYLINT_DUP),
        Check("lint.auth-signals", "T0", cmd=LINT_AUTH_SIGNALS),
        Check("lint.arch-boundaries", "T0", cmd=LINT_ARCH_BOUNDARIES),
        Check(
            "spec.mode-b",
            "T0",
            cmd=SPEC_MODE_B,
            env={"BASE_REF": ctx.base_ref, "GH_PR_BODY": ""},
        ),
    ]
    return checks


def _t1_checks(ctx: Context) -> list[Check]:
    checks = []
    tests = mapped_unit_tests(ctx.root, ctx.changed)
    if tests and len(tests) <= UNIT_MAP_CAP:
        xdist = " -n auto --dist worksteal" if len(tests) > UNIT_MAP_XDIST_THRESHOLD else ""
        cmd = f"{UV} pytest -p no:cacheprovider -q{xdist} {_quote_all(tests)}"
        checks.append(Check("unit.mapped", "T1", cmd=cmd, exclusive=bool(xdist)))
    elif tests and "T2" not in ctx.selected:
        checks.append(Check("unit.mapped", "T1", cmd=UNIT_FULL, exclusive=True))
    if any(_needs_arch_contracts(p) for p in ctx.changed):
        checks.append(
            Check(
                "arch.contracts",
                "T1",
                cmd=ARCH_CONTRACTS,
                env={"APM_ALLOW_PROVISIONAL_BASELINES": "0"},
            )
        )
    if any(p.startswith(DASHBOARD_PREFIX) for p in ctx.changed):
        checks.append(Check("dashboard.node", "T1", cmd=DASHBOARD_NODE))
    if any(_is_self_check(p) for p in ctx.changed):
        checks.append(Check("apm.self-check", "T1", cmd=APM_SELF_CHECK))
    if any(_is_spec(p) for p in ctx.changed):
        checks.append(Check("spec.orphans", "T1", cmd=SPEC_ORPHAN))
        checks.append(Check("spec.conformance", "T1", cmd=SPEC_SUITE))
    return checks


def _t3_checks(ctx: Context) -> list[Check]:
    checks = [
        Check(
            f"lifecycle.smoke-{shard}",
            "T3",
            cmd=LIFECYCLE_SMOKE,
            env={"APM_E2E_TESTS": "1", "LIFECYCLE_SHARD": shard},
            exclusive=True,
        )
        for shard in ("1", "2")
    ]
    integration = sorted(
        p
        for p in ctx.changed
        if p.startswith("tests/integration/")
        and p.rsplit("/", 1)[-1].startswith("test_")
        and p.endswith(".py")
        and (ctx.root / p).is_file()
    )
    if integration:
        cmd = f"{UV} python -m pytest -p no:cacheprovider -q --strict-markers {_quote_all(integration)}"
        checks.append(
            Check("integration.changed", "T3", cmd=cmd, env={"APM_E2E_TESTS": "1"}, exclusive=True)
        )
    return checks


def build_checks(tier: str, ctx: Context) -> list[Check]:
    """Concrete checks for *tier* given the runner context."""
    if tier == "T0":
        return _t0_checks(ctx)
    if tier == "T1":
        return _t1_checks(ctx)
    if tier == "T2":
        return [Check("unit.full", "T2", cmd=UNIT_FULL, exclusive=True)]
    if tier == "T3":
        return _t3_checks(ctx)
    raise ValueError(f"unknown tier {tier!r}")
