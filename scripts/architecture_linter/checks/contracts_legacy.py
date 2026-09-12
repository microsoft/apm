"""Legacy-shell parity checks restored for the ``contracts_tests`` rule group.

Three enforcement units of ``scripts/lint-architecture-boundaries.sh`` lost
their successor when the shell linter was ported to the fact engine. They are
restored here, one function per legacy block, and registered as guard-less
structural rules by
:mod:`scripts.architecture_linter.groups.contracts_tests`:

* :func:`check_ado_lock_coordinates` -- legacy AC14 (shell L1215-1225): ADO
  coordinates are derived by ``DependencyReference``, never persisted in the
  lockfile or re-split in the marketplace ref resolver.
* :func:`check_ref_recheck_test_tree` -- the ``tests/`` half of the
  ref-recheck owner guard (shell L575-576). The ``src/`` half (owner def in
  ``drift.py`` plus its two resolver consumers) stays with the install
  resolution owner; this module deliberately does not restate it.
* :func:`check_object_git_fields` -- the duplicate scan and fixture guard of
  the object-form Git dependency field block (shell L857-876). The parser
  wiring half of that block (shell L877-880, ``reject_unknown_git_fields``
  called with ``parent=True``/``parent=False`` in ``reference.py``) belongs to
  the dependency-parser owner and is likewise not restated here.

Exemption handling is ported per block, not uniformly, because the legacy
shell was not uniform. Blocks whose greps piped through
``grep -v 'architecture-authority-exempt:'`` stay exemptible; blocks whose
greps did not (AC14 and the ref-recheck test-tree ban) must NOT become
silently exemptible, so their scans pass ``respect_exempt=False``.

Every read goes through the shared :class:`FactsProvider` caches: nothing here
opens a file, walks a directory, spawns a process, or parses/walks an AST.
Tree-wide scans first consult the memoized source text for a cheap token
pre-filter -- a line can only match the enforced regex if the file text
contains one of the pre-filter tokens -- so a clean tree costs one substring
sweep over already-cached bytes instead of parsing every file under
``src/`` and ``tests/``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import (
    EXEMPT_MARKER,
    checked_facts,
    line_pattern_violations,
    merge_findings,
    require_text,
    violation,
)
from scripts.architecture_linter.models import Violation

# --------------------------------------------------------------------------
# Stable, guard-less rule IDs (no owner-registry guard is allocated to any of
# them; the five contracts_tests owner guards keep their single allocation).
# --------------------------------------------------------------------------
RULE_ADO_LOCK_COORDINATES = "contracts-tooling-ado-lock-coordinates"
RULE_REF_RECHECK_TEST_TREE = "contracts-tests-ref-recheck-test-tree"
RULE_OBJECT_GIT_FIELDS = "contracts-tests-object-git-fields"


# --------------------------------------------------------------------------
# One canonical pre-filtered lexical tree scan (the `grep -rEn` primitive).
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class TreeScan:
    """One legacy recursive grep: where to look, what to reject, how to exempt.

    `prefilter` holds tokens of which any matching line must contain at least
    one, letting the scan skip a file on its cached text alone. It is a
    correctness-preserving superset of `pattern`: widening `pattern` without
    widening `prefilter` would silently drop findings, so the two are declared
    side by side in one frozen value object rather than at separate call sites.
    """

    rule_id: str
    prefixes: tuple[str, ...]
    pattern: re.Pattern[str]
    prefilter: tuple[str, ...]
    message: str
    respect_exempt: bool
    excluded_paths: frozenset[str] = field(default_factory=frozenset)
    excluded_names: frozenset[str] = field(default_factory=frozenset)


def _candidate_paths(provider: FactsProvider, scan: TreeScan) -> tuple[str, ...]:
    """Select the scan's Python inventory paths, minus its legacy exclusions."""
    return tuple(
        path
        for path in provider.inventory
        if path.endswith(".py")
        and path.startswith(scan.prefixes)
        and path not in scan.excluded_paths
        and PurePosixPath(path).name not in scan.excluded_names
    )


def run_tree_scan(provider: FactsProvider, scan: TreeScan) -> tuple[Violation, ...]:
    """Report every line matching `scan` across its tree, fail-closed on reads.

    These blocks are purely lexical bans, so they read cached lines instead of
    routing through ``checked_facts``: requiring a successful Python parse would
    be stricter than the legacy grep and would turn an intentionally malformed
    fixture into a false positive. An unreadable file still fails closed, which
    is the one place the port is deliberately stricter than ``grep -r``.
    """
    findings: list[Violation] = []
    for path in _candidate_paths(provider, scan):
        text, read_error = provider.source_cache.read(path)
        if text is None:
            findings.append(
                violation(scan.rule_id, path, f"required source unavailable: {read_error}")
            )
            continue
        if not any(token in text for token in scan.prefilter):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if scan.respect_exempt and EXEMPT_MARKER in line:
                continue
            match = scan.pattern.search(line)
            if match is not None:
                findings.append(
                    violation(
                        scan.rule_id,
                        path,
                        scan.message,
                        line=number,
                        column=match.start() + 1,
                    )
                )
    return tuple(findings)


# --------------------------------------------------------------------------
# AC14 ADO lock-coordinate authority (legacy shell L1215-1225).
#
#   ! grep -q 'with_derived_provider_coordinates' deps/lockfile.py
#   | grep -Eq 'ado_(organization|project|repo)' deps/lockfile.py
#   | ! grep -q 'DependencyReference.canonical_ado_coordinates' ref_resolver.py
#   | grep -Eq '(self\.)?repo_url\.split\(' deps/lockfile.py
#   | grep -Eq 'owner_repo\.split\(' ref_resolver.py
#
# None of the five greps filtered the exemption marker, so none of the five
# subconditions may be waived by an inline marker here either.
# --------------------------------------------------------------------------
_LOCKFILE = "src/apm_cli/deps/lockfile.py"
_REF_RESOLVER = "src/apm_cli/marketplace/ref_resolver.py"
_ADO_LOCK_MESSAGE = "ADO coordinates must be derived by DependencyReference, never persisted"

_ADO_PERSISTED_FIELD_BAN = re.compile(r"ado_(organization|project|repo)")
_LOCKFILE_SPLIT_BAN = re.compile(r"(self\.)?repo_url\.split\(")
_RESOLVER_SPLIT_BAN = re.compile(r"owner_repo\.split\(")


def check_ado_lock_coordinates(provider: FactsProvider) -> tuple[Violation, ...]:
    """ADO coordinates stay derived by DependencyReference, never persisted."""
    rule_id = RULE_ADO_LOCK_COORDINATES
    unavailable: list[Violation] = []
    for path in (_LOCKFILE, _REF_RESOLVER):
        _, problems = checked_facts(provider, path, rule_id, require_python=True)
        unavailable.extend(problems)
    if unavailable:
        return tuple(unavailable)

    return merge_findings(
        require_text(
            provider,
            rule_id=rule_id,
            path=_LOCKFILE,
            needles=("with_derived_provider_coordinates",),
            message=f"{_ADO_LOCK_MESSAGE}; lock entries must derive provider coordinates",
        ),
        require_text(
            provider,
            rule_id=rule_id,
            path=_REF_RESOLVER,
            needles=("DependencyReference.canonical_ado_coordinates",),
            message=f"{_ADO_LOCK_MESSAGE}; resolver must ask DependencyReference",
        ),
        line_pattern_violations(
            provider,
            rule_id=rule_id,
            paths=(_LOCKFILE,),
            pattern=_ADO_PERSISTED_FIELD_BAN,
            message=f"{_ADO_LOCK_MESSAGE}; persisted ADO coordinate field",
            exempt_marker=None,
        ),
        line_pattern_violations(
            provider,
            rule_id=rule_id,
            paths=(_LOCKFILE,),
            pattern=_LOCKFILE_SPLIT_BAN,
            message=f"{_ADO_LOCK_MESSAGE}; parallel repo_url split in the lockfile",
            exempt_marker=None,
        ),
        line_pattern_violations(
            provider,
            rule_id=rule_id,
            paths=(_REF_RESOLVER,),
            pattern=_RESOLVER_SPLIT_BAN,
            message=f"{_ADO_LOCK_MESSAGE}; parallel owner_repo split in the resolver",
            exempt_marker=None,
        ),
    )


# --------------------------------------------------------------------------
# Ref-recheck owner guard, tests/ half (legacy shell L575-576).
# --------------------------------------------------------------------------
_REF_RECHECK_OWNER = "src/apm_cli/drift.py"

_REF_RECHECK_TEST_SCAN = TreeScan(
    rule_id=RULE_REF_RECHECK_TEST_TREE,
    prefixes=("tests/",),
    pattern=re.compile(r"def _force_semver_resolve|def should_force_ref_recheck"),
    prefilter=("_force_semver_resolve", "should_force_ref_recheck"),
    message=(
        f"Existing-path ref rechecks must use {_REF_RECHECK_OWNER}"
        "::should_force_ref_recheck; the test tree must not define a parallel "
        "ref-recheck decision"
    ),
    respect_exempt=False,
)


def check_ref_recheck_test_tree(provider: FactsProvider) -> tuple[Violation, ...]:
    """The test tree must not redefine the canonical ref-recheck decision."""
    return run_tree_scan(provider, _REF_RECHECK_TEST_SCAN)


# --------------------------------------------------------------------------
# Object-form Git dependency fields (legacy shell L857-876).
#
#   dependency_field_duplicate_hits: grep -rEn --include='*.py'
#       'def reject_unknown_git_fields|_(REMOTE|PARENT)_GIT_DEPENDENCY_FIELDS'
#       src tests, minus the owner file and exemption-marked lines.
#   fixture_dependency_field_hits: grep -En
#       'reject_unknown_fields|_(REMOTE|PARENT)?_?GIT_DEPENDENCY_FIELDS'
#       tests/utils/local_package.py, minus exemption-marked lines.
#
# Both greps filtered the exemption marker, so both stay exemptible.
# --------------------------------------------------------------------------
_GIT_FIELD_OWNER = "src/apm_cli/models/dependency/object_fields.py"
_GIT_FIELD_FIXTURE = "tests/utils/local_package.py"
_GIT_FIELD_MESSAGE = "Object-form Git dependency fields must come from the product parser"

_GIT_FIELD_DUPLICATE_SCAN = TreeScan(
    rule_id=RULE_OBJECT_GIT_FIELDS,
    prefixes=("src/", "tests/"),
    pattern=re.compile(r"def reject_unknown_git_fields|_(REMOTE|PARENT)_GIT_DEPENDENCY_FIELDS"),
    prefilter=("reject_unknown_git_fields", "_GIT_DEPENDENCY_FIELDS"),
    message=f"{_GIT_FIELD_MESSAGE}; owner is {_GIT_FIELD_OWNER}",
    respect_exempt=True,
    excluded_paths=frozenset({_GIT_FIELD_OWNER}),
)

_GIT_FIELD_FIXTURE_BAN = re.compile(
    r"reject_unknown_fields|_(REMOTE|PARENT)?_?GIT_DEPENDENCY_FIELDS"
)


def check_object_git_fields(provider: FactsProvider) -> tuple[Violation, ...]:
    """Field vocabulary stays in the parser; fixtures never restate it."""
    return merge_findings(
        run_tree_scan(provider, _GIT_FIELD_DUPLICATE_SCAN),
        line_pattern_violations(
            provider,
            rule_id=RULE_OBJECT_GIT_FIELDS,
            paths=(_GIT_FIELD_FIXTURE,),
            pattern=_GIT_FIELD_FIXTURE_BAN,
            message=(
                f"{_GIT_FIELD_MESSAGE}; the local-package fixture must consume {_GIT_FIELD_OWNER}"
            ),
            exempt_marker=EXEMPT_MARKER,
        ),
    )


# --------------------------------------------------------------------------
# Catalog surface consumed by the contracts_tests rule group.
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class LegacyCheck:
    """One restored legacy block: stable rule ID, description, and callable."""

    rule_id: str
    description: str
    check: Callable[[FactsProvider], tuple[Violation, ...]]


LEGACY_CHECKS: Sequence[LegacyCheck] = (
    LegacyCheck(
        RULE_ADO_LOCK_COORDINATES,
        "ADO lock coordinates are derived by DependencyReference, never persisted or re-split.",
        check_ado_lock_coordinates,
    ),
)
