"""Guard local bundle compile staging as lockfile-proven input, never a claim (#3705).

Staged content is hash-bound by the deployed-file hash owner, and ``apm audit``
and ``apm compile`` verify it through ``LockFile.staged_hash_drift`` (#3718).
"""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.install_deployment_shared import (
    _facts_for,
    _present,
    _summary,
)
from scripts.architecture_linter.checks.lexical_shared import python_paths
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Violation

GUARD = "install-deployment-bundle-compile-staging"
LOCKFILE = "src/apm_cli/deps/lockfile.py"
LEDGER = "src/apm_cli/core/deployment_ledger.py"
SERVICES = "src/apm_cli/install/services.py"
HANDLER = "src/apm_cli/install/local_bundle_handler.py"
DISCOVERY = "src/apm_cli/primitives/discovery.py"
PRUNE = "src/apm_cli/commands/prune.py"
AUDIT = "src/apm_cli/policy/ci_checks.py"
COMPILE = "src/apm_cli/commands/compile/cli.py"
PHASES = "src/apm_cli/install/phases/lockfile.py"
_STAGED_HASH_WRITERS = frozenset({LOCKFILE, LEDGER})
_STAGED_HASH_WRITE = re.compile(
    r"\.local_staged_file_hashes(\s*\[[^\]]*\])?\s*=(?!=)"
    r"|\.local_staged_file_hashes\.(update|setdefault|pop|clear)\("
)

_REQUIRED: dict[str, tuple[tuple[str, str], ...]] = {
    LOCKFILE: (
        ("local_staged_files: list[str] = field(default_factory=list)", "own the staging record"),
        ("def local_staging_slugs(self) -> list[str]:", "own staging-root proof"),
        ("def retire_local_staging(self, slugs: Collection[str]) -> None:", "own retirement"),
        ("def local_staging_slug(path: str) -> str | None:", "own the staging path shape"),
        ("def absent_staging_slugs(self, modules_dir: Path) -> list[str]:", "own stale roots"),
        (
            "local_staged_file_hashes: dict[str, str] = field(default_factory=dict)",
            "own the staged content hashes",
        ),
        (
            "def staged_hash_drift(self, project_root: Path) -> list[tuple[str, str, str]]:",
            "own staged drift verification",
        ),
        ("actual = compute_file_hash(file_path)", "verify with the deployed-file hash owner"),
        (
            "def carry_local_staging_from(self, other: LockFile) -> None:",
            "carry staging records and hashes together",
        ),
    ),
    LEDGER: (
        (
            "path for path in prior_bundle_paths if local_staging_slug(path) is not None",
            "migrate every prior bundle staging claim",
        ),
        (
            "set(lockfile.local_staged_files).union(staged, retired_paths)",
            "record staging apart from claims",
        ),
        ("**(staged_hashes or {}),", "record staged content hashes"),
        ("**(staged_fallback_hashes or {}),", "bind collision-skipped staging"),
    ),
    SERVICES: (
        (
            "claims = staged_files if _staged else deployed_files",
            "report staged output apart from deployed claims",
        ),
        (
            "(staged_hashes if _staged else deployed_hashes)[record] = (",
            "hash staged output with the deployed-file hash owner",
        ),
        (
            'staged_fallback_hashes[record] = f"sha256:{desired_hash}"',
            "bind collision-skipped staging",
        ),
    ),
    HANDLER: (
        ('staged_instructions = result.get("staged_files", [])', "read staging from the service"),
        ("staged=staged_instructions", "record staging through the ledger owner"),
        (
            'staged_hashes=result.get("staged_file_hashes", {})',
            "record staged hashes through the ledger owner",
        ),
    ),
    DISCOVERY: (
        ("for slug in lock.local_staging_slugs():", "prove staging roots via the lockfile owner"),
    ),
    PRUNE: (
        ("lockfile.absent_staging_slugs(apm_modules_dir)", "plan absent staging roots"),
        (
            "lockfile.retire_local_staging([*removed_packages, *absent_staging])",
            "retire pruned and absent staging",
        ),
    ),
    PHASES: (
        (
            "lockfile.carry_local_staging_from(self.ctx.existing_lockfile)",
            "carry staging through the lockfile owner",
        ),
    ),
    AUDIT: (
        (
            "staged_drift = lock.staged_hash_drift(project_root)",
            "audit staged drift through the lockfile owner",
        ),
    ),
    COMPILE: (
        (
            "lock.staged_hash_drift(project_root)",
            "warn on staged drift through the lockfile owner",
        ),
        ("_report_staged_hash_drift(logger, _src)", "check staged drift before compiling"),
    ),
}

_PATH_HEURISTIC = re.compile(r'"/\.apm/instructions/"|parts\[2\] == "\.apm"')


def check_bundle_compile_staging(provider: FactsProvider) -> tuple[Violation, ...]:
    """Require one staging record owner and reject path-shaped re-derivations."""
    findings: list[Violation] = []
    for path, needles in _REQUIRED.items():
        facts, failures = _facts_for(provider, path, GUARD)
        findings.extend(failures)
        if failures:
            continue
        for needle, purpose in needles:
            if not _present(facts, needle):
                findings.append(_summary(GUARD, path, f"Bundle compile staging must {purpose}"))
        if path in (HANDLER, DISCOVERY):
            for number, line in enumerate(getattr(facts, "lines", ()), start=1):
                if _PATH_HEURISTIC.search(line):
                    findings.append(
                        _summary(
                            GUARD,
                            path,
                            f"line {number}: derive staging from LockFile.local_staging_slugs "
                            "or the service result, not from path shape",
                        )
                    )
    for path in python_paths(provider, under="src/apm_cli/"):
        if path in _STAGED_HASH_WRITERS:
            continue
        facts = provider.file_facts(path)
        for number, line in enumerate(getattr(facts, "lines", ()), start=1):
            if _STAGED_HASH_WRITE.search(line):
                findings.append(
                    _summary(
                        GUARD,
                        path,
                        f"line {number}: write local_staged_file_hashes only through "
                        "the lockfile or deployment-ledger owner",
                    )
                )
    return tuple(findings)
