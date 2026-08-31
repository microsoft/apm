"""Mutation coverage for the in-process repository cache-identity authority.

The ``transport-platform-git-cache-identity`` registered rule owns these
semantics. Its mutation matrix covers the exact evasions that a lexical check
misses: truncation hidden behind a renamed helper and truncation applied after
canonical normalization.

Every test here drives the registered rule through the real engine
(:func:`run_selected_rules`) against a full filesystem copy of this repository,
mutated one defect at a time.
"""

from __future__ import annotations

import ast
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import pytest

from scripts.architecture_linter.inventory import EXCLUDED_ROOTS
from scripts.architecture_linter.runner import registered_rules, run_selected_rules

pytestmark = [
    pytest.mark.integration,
    # One module-scoped fixture pays for a single filesystem copy of the
    # repository and one linter run per mutation. `--dist loadgroup` (the
    # scheduler this repo's sharded integration runs use) is the only
    # scheduler that honors `xdist_group`; without it these tests could be
    # split across workers and each worker would recompute the whole sweep.
    pytest.mark.xdist_group(name="architecture_cache_identity_mutations"),
]

ROOT = Path(__file__).resolve().parents[2]
RULE_ID = "transport-platform-git-cache-identity"

SHARED_CACHE = "src/apm_cli/deps/shared_clone_cache.py"
TIERED_RESOLVER = "src/apm_cli/deps/tiered_ref_resolver.py"

# The two files this change owns. Both are held to the repository's 1000-line
# module budget (see test_architecture_module_line_budget in
# tests/unit/scripts/test_architecture_runner.py for the repo-wide version of
# this same assertion); neither may reconstruct AST shape or touch the
# filesystem. ``_check_git_cache_identity`` lives in its own cohesive module
# (split out of the former ``transport_platform_analyzers.py`` monolith)
# specifically so this narrow boundary stays easy to audit.
OWNED_MODULES = (
    "scripts/architecture_linter/checks/transport_cache_identity.py",
    "scripts/architecture_linter/checks/repository_cache_identity.py",
)
MAX_MODULE_LINES = 1000

# Calls and attribute accesses that would mean the analyzer stopped riding the
# engine's one shared read/parse/traversal and started doing its own.
BANNED_CALLEES = frozenset(
    {
        "ast.parse",
        "ast.walk",
        "ast.iter_child_nodes",
        "ast.NodeVisitor",
        "NodeVisitor",
        "open",
        "os.walk",
        "os.listdir",
        "os.scandir",
        "subprocess.run",
        "subprocess.Popen",
        "subprocess.check_output",
        "subprocess.check_call",
    }
)
BANNED_METHODS = frozenset(
    {"read_text", "read_bytes", "write_text", "write_bytes", "rglob", "glob", "iterdir"}
)

_IDENTITY_DEF = '''def _repository_cache_identity(dep_ref: DependencyReference) -> str:
    """Return the full normalized repository identity shared by all cache tiers."""
    from ..cache.url_normalize import normalize_repo_url

    return normalize_repo_url(dep_ref.to_github_url())
'''

_RENAMED_INDIRECT_IDENTITY = '''def _canonical_repository_url(dep_ref: DependencyReference) -> str:
    """Renamed indirection that still spells the canonical composition."""
    from ..cache.url_normalize import normalize_repo_url

    return normalize_repo_url(dep_ref.to_github_url())


def _repository_cache_identity(dep_ref: DependencyReference) -> str:
    """Return the full normalized repository identity shared by all cache tiers."""
    identity = _canonical_repository_url(dep_ref)
    return identity.rsplit("/", 1)[-1]
'''


# ---------------------------------------------------------------------------
# Mutation catalog: one entry per helper diagnostic and fail-closed case.
# ---------------------------------------------------------------------------


def _rewrite(root: Path, relative: str, old: str, new: str) -> None:
    """Replace the first occurrence of `old` in a sandbox file.

    Asserting the anchor is present keeps a mutation from silently becoming a
    no-op if the guarded source is refactored -- a no-op mutation would make
    the corresponding "the rule catches this" test vacuously pass.
    """
    path = root / relative
    source = path.read_text(encoding="utf-8")
    assert old in source, f"mutation anchor vanished from {relative}: {old!r}"
    path.write_text(source.replace(old, new, 1), encoding="utf-8")


def _append(root: Path, relative: str, tail: str) -> None:
    """Append `tail` to a sandbox file without disturbing what precedes it."""
    path = root / relative
    path.write_text(path.read_text(encoding="utf-8") + tail, encoding="utf-8")


@dataclass(frozen=True)
class Mutation:
    """One defect injected into the sandbox, and what the rule must say."""

    apply: Callable[[Path], None]
    expected: str


MUTATIONS: Mapping[str, Mutation] = {
    # -- SharedCloneCache.get_or_clone: exact assignment/call counts ---------
    "shared-post-normalization-truncation": Mutation(
        apply=lambda root: _rewrite(
            root,
            SHARED_CACHE,
            "        repository = normalize_repo_url(repository_url)\n",
            "        repository = normalize_repo_url(repository_url)\n"
            "        repository = repository.rsplit('/', 1)[-1]\n",
        ),
        expected="without post-normalization transforms",
    ),
    "shared-post-normalization-augassign": Mutation(
        apply=lambda root: _rewrite(
            root,
            SHARED_CACHE,
            "        repository = normalize_repo_url(repository_url)\n",
            "        repository = normalize_repo_url(repository_url)\n"
            '        repository += "/rogue"\n',
        ),
        expected="without post-normalization transforms",
    ),
    "shared-post-normalization-match-capture": Mutation(
        apply=lambda root: _rewrite(
            root,
            SHARED_CACHE,
            "        repository = normalize_repo_url(repository_url)\n",
            "        repository = normalize_repo_url(repository_url)\n"
            "        match repository_url:\n"
            "            case repository:\n"
            "                pass\n",
        ),
        expected="without post-normalization transforms",
    ),
    "shared-post-normalization-destructuring": Mutation(
        apply=lambda root: _rewrite(
            root,
            SHARED_CACHE,
            "        repository = normalize_repo_url(repository_url)\n",
            "        repository, *_discarded = normalize_repo_url(repository_url)\n",
        ),
        expected="without post-normalization transforms",
    ),
    "shared-key-nested-nonlocal-rebinding": Mutation(
        apply=lambda root: _rewrite(
            root,
            SHARED_CACHE,
            "        key = (repository, ref)\n",
            "        key = (repository, ref)\n"
            "        def rewrite_key():\n"
            "            nonlocal key\n"
            "            key = (repository.rsplit('/', 1)[-1], ref)\n"
            "        rewrite_key()\n",
        ),
        expected="cache key must be the direct (repository, ref) tuple",
    ),
    "shared-normalizer-import-provenance": Mutation(
        apply=lambda root: _rewrite(
            root,
            SHARED_CACHE,
            "from ..cache.url_normalize import cache_shard_key, normalize_repo_url\n",
            "from ..cache.url_normalize import cache_shard_key\n"
            "from ..cache.url_normalize import cache_shard_key as normalize_repo_url\n",
        ),
        expected="without post-normalization transforms",
    ),
    "shared-key-not-the-direct-tuple": Mutation(
        apply=lambda root: _rewrite(
            root,
            SHARED_CACHE,
            "        key = (repository, ref)\n",
            "        key = (repository_shard, ref)\n",
        ),
        expected="cache key must be the direct (repository, ref) tuple",
    ),
    "shared-bare-lookup-truncated": Mutation(
        apply=lambda root: _rewrite(
            root,
            SHARED_CACHE,
            "self._find_repo_bare(repository)",
            "self._find_repo_bare(repository_shard)",
        ),
        expected="Tier-0 bare lookup must consume the direct normalized repository identity",
    ),
    "shared-get-or-clone-missing": Mutation(
        apply=lambda root: _rewrite(
            root, SHARED_CACHE, "    def get_or_clone(\n", "    def get_or_clone_renamed(\n"
        ),
        expected="SharedCloneCache.get_or_clone is missing",
    ),
    "shared-get-or-clone-duplicate-effective-definition": Mutation(
        apply=lambda root: _rewrite(
            root,
            SHARED_CACHE,
            "    def _find_repo_bare(self, repository_url: str) -> Path | None:\n",
            "    def get_or_clone(self, *args, **kwargs):\n"
            "        return Path('/tmp/rogue')\n"
            "\n"
            "    def _find_repo_bare(self, repository_url: str) -> Path | None:\n",
        ),
        expected="SharedCloneCache.get_or_clone has duplicate definitions",
    ),
    # -- _repository_cache_identity: the direct composition -----------------
    "identity-renamed-indirect-truncation": Mutation(
        apply=lambda root: _rewrite(
            root, TIERED_RESOLVER, _IDENTITY_DEF, _RENAMED_INDIRECT_IDENTITY
        ),
        expected="without indirect truncation",
    ),
    "identity-keyword-truncation": Mutation(
        apply=lambda root: _rewrite(
            root,
            TIERED_RESOLVER,
            "    return normalize_repo_url(dep_ref.to_github_url())\n",
            "    return normalize_repo_url(dep_ref.to_github_url(), segments=2)\n",
        ),
        expected="without indirect truncation",
    ),
    "identity-function-missing": Mutation(
        apply=lambda root: _rewrite(
            root,
            TIERED_RESOLVER,
            "def _repository_cache_identity(dep_ref: DependencyReference) -> str:",
            "def _renamed_cache_identity(dep_ref: DependencyReference) -> str:",
        ),
        expected="_repository_cache_identity is missing",
    ),
    "identity-duplicate-effective-definition": Mutation(
        apply=lambda root: _append(
            root,
            TIERED_RESOLVER,
            "\n\ndef _repository_cache_identity(dep_ref):\n    return dep_ref.repo_url\n",
        ),
        expected="_repository_cache_identity has duplicate definitions",
    ),
    # -- L0PerRunCache.try_resolve ------------------------------------------
    "l0-try-resolve-missing": Mutation(
        apply=lambda root: _rewrite(
            root,
            TIERED_RESOLVER,
            "    def try_resolve(self, dep_ref: DependencyReference, ref: str) -> str | None:\n"
            "        return self.cache.get(_repository_cache_identity(dep_ref), ref)\n",
            "    def lookup(self, dep_ref: DependencyReference, ref: str) -> str | None:\n"
            "        return self.cache.get(_repository_cache_identity(dep_ref), ref)\n",
        ),
        expected="L0PerRunCache.try_resolve is missing",
    ),
    "l0-lookup-truncated": Mutation(
        apply=lambda root: _rewrite(
            root,
            TIERED_RESOLVER,
            "        return self.cache.get(_repository_cache_identity(dep_ref), ref)\n",
            "        return self.cache.get(dep_ref.repo_url, ref)\n",
        ),
        expected="L0 lookup must call cache.get(_repository_cache_identity(dep_ref), ref)",
    ),
    # -- TieredRefResolver.resolve ------------------------------------------
    "resolve-missing": Mutation(
        apply=lambda root: _rewrite(
            root,
            TIERED_RESOLVER,
            "    def resolve(self, repo_ref: str | DependencyReference) -> ResolvedReference:\n",
            "    def resolve_ref(self, repo_ref: str | DependencyReference) -> ResolvedReference:\n",
        ),
        expected="TieredRefResolver.resolve is missing",
    ),
    "resolve-key-truncated": Mutation(
        apply=lambda root: _rewrite(
            root,
            TIERED_RESOLVER,
            "        key = (_repository_cache_identity(dep_ref), ref)\n",
            "        key = (dep_ref.repo_url, ref)\n",
        ),
        expected="resolver coalescing key must be the direct",
    ),
    "resolve-duplicate-effective-definition": Mutation(
        apply=lambda root: _rewrite(
            root,
            TIERED_RESOLVER,
            "    def _dispatch(self, dep_ref: DependencyReference, ref: str) -> str | None:\n",
            "    def resolve(self, repo_ref):\n"
            "        return repo_ref\n"
            "\n"
            "    def _dispatch(self, dep_ref: DependencyReference, ref: str) -> str | None:\n",
        ),
        expected="TieredRefResolver.resolve has duplicate definitions",
    ),
    # -- TieredRefResolver.seed ---------------------------------------------
    "seed-missing": Mutation(
        apply=lambda root: _rewrite(
            root,
            TIERED_RESOLVER,
            "    def seed(self, repo_ref: str | DependencyReference, ref: str, sha: str) -> bool:\n",
            "    def prime(self, repo_ref: str | DependencyReference, ref: str, sha: str) -> bool:\n",
        ),
        expected="TieredRefResolver.seed is missing",
    ),
    "seed-put-truncated": Mutation(
        apply=lambda root: _rewrite(
            root,
            TIERED_RESOLVER,
            "        self._cache.put(_repository_cache_identity(dep_ref), ref, sha.lower())\n",
            "        self._cache.put(dep_ref.repo_url, ref, sha.lower())\n",
        ),
        expected="lockfile seed must call _cache.put(",
    ),
    # -- Fail-closed cases ---------------------------------------------------
    "shared-source-missing": Mutation(
        apply=lambda root: (root / SHARED_CACHE).unlink(),
        expected="configured cache-identity owner path is missing or not a regular file",
    ),
    "tiered-source-missing": Mutation(
        apply=lambda root: (root / TIERED_RESOLVER).unlink(),
        expected="configured cache-identity owner path is missing or not a regular file",
    ),
    "shared-source-unparseable": Mutation(
        apply=lambda root: _append(root, SHARED_CACHE, "\ndef broken(:\n    pass\n"),
        expected="cannot parse configured cache-identity owner source",
    ),
    "tiered-source-unparseable": Mutation(
        apply=lambda root: _append(root, TIERED_RESOLVER, "\ndef broken(:\n    pass\n"),
        expected="cannot parse configured cache-identity owner source",
    ),
    # -- The retained lexical half still has to fire on its own defects ------
    "lexical-retired-cache-url-helper": Mutation(
        apply=lambda root: _append(
            root,
            "src/apm_cli/deps/github_downloader.py",
            "\n\ndef to_repository_cache_url(url):\n    return url\n",
        ),
        expected="Repository cache keys must stay owned by cache/url_normalize.py",
    ),
}


# ---------------------------------------------------------------------------
# One filesystem copy, one linter run per mutation, computed once.
# ---------------------------------------------------------------------------


class MutationRun(NamedTuple):
    """What the registered rule reported for one sandbox."""

    rule_messages: tuple[str, ...]
    rule_errors: tuple[str, ...]


def _run_rule(sandbox: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    report = run_selected_rules(sandbox, [RULE_ID])
    messages = tuple(
        sorted({item.message for item in report.violations if item.rule_id == RULE_ID})
    )
    errors = tuple(
        sorted(
            failure.message
            for failure in report.failures
            if failure.stage in (f"rule:{RULE_ID}", f"rule-result:{RULE_ID}")
        )
    )
    return messages, errors


@pytest.fixture(scope="module")
def cache_identity_runs(tmp_path_factory: pytest.TempPathFactory) -> Mapping[str, MutationRun]:
    """Lint one full repository copy per mutation, plus one clean baseline.

    The copy is made once and restored between mutations by rewriting only
    the two owner modules, so the whole sweep pays for exactly one
    `copytree` rather than one per case.
    """
    sandbox = tmp_path_factory.mktemp("cache-identity-repo") / "repo"
    shutil.copytree(ROOT, sandbox, ignore=shutil.ignore_patterns(*EXCLUDED_ROOTS))

    pristine = {
        relative: (sandbox / relative).read_text(encoding="utf-8")
        for relative in (SHARED_CACHE, TIERED_RESOLVER, "src/apm_cli/deps/github_downloader.py")
    }

    def restore() -> None:
        for relative, source in pristine.items():
            (sandbox / relative).write_text(source, encoding="utf-8")

    runs: dict[str, MutationRun] = {}
    for name in ("clean", *MUTATIONS):
        restore()
        if name != "clean":
            MUTATIONS[name].apply(sandbox)
        rule_messages, rule_errors = _run_rule(sandbox)
        runs[name] = MutationRun(
            rule_messages=rule_messages,
            rule_errors=rule_errors,
        )
    restore()
    return runs


# ---------------------------------------------------------------------------
# The two regressions that motivated this change.
# ---------------------------------------------------------------------------


def test_post_normalization_truncation_in_get_or_clone_is_caught(
    cache_identity_runs: Mapping[str, MutationRun],
) -> None:
    """A truncating `rsplit` after the normalizer must fail the rule.

    The lexical half still matches, because
    `repository = normalize_repo_url(repository_url)` is untouched -- only
    the structural half sees the second binding.
    """
    run = cache_identity_runs["shared-post-normalization-truncation"]

    assert any("without post-normalization transforms" in item for item in run.rule_messages)
    assert run.rule_errors == ()
    assert "repository = normalize_repo_url(repository_url)" in (ROOT / SHARED_CACHE).read_text(
        encoding="utf-8"
    )


def test_post_normalization_augassign_in_get_or_clone_is_caught(
    cache_identity_runs: Mapping[str, MutationRun],
) -> None:
    """An augmented rebinding after normalization cannot alter cache identity."""
    run = cache_identity_runs["shared-post-normalization-augassign"]

    assert any("without post-normalization transforms" in item for item in run.rule_messages)
    assert run.rule_errors == ()


def test_post_normalization_match_capture_in_get_or_clone_is_caught(
    cache_identity_runs: Mapping[str, MutationRun],
) -> None:
    """A structural-pattern capture cannot silently replace normalized identity."""
    run = cache_identity_runs["shared-post-normalization-match-capture"]

    assert any("without post-normalization transforms" in item for item in run.rule_messages)
    assert run.rule_errors == ()


def test_renamed_indirect_truncation_in_identity_helper_is_caught(
    cache_identity_runs: Mapping[str, MutationRun],
) -> None:
    """Truncation behind a renamed helper must fail the rule.

    The canonical line the lexical half greps for
    (`return normalize_repo_url(dep_ref.to_github_url())`) is still present
    in the mutated file -- it just moved into the renamed indirection.
    """
    run = cache_identity_runs["identity-renamed-indirect-truncation"]

    assert any("without indirect truncation" in item for item in run.rule_messages)
    assert run.rule_errors == ()
    assert "return normalize_repo_url(dep_ref.to_github_url())" in _RENAMED_INDIRECT_IDENTITY


# ---------------------------------------------------------------------------
# Consumer coverage and the full registered-rule diagnostic surface.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["l0-try-resolve-missing", "resolve-missing", "seed-missing"],
)
def test_missing_identity_consumer_fails_closed(
    cache_identity_runs: Mapping[str, MutationRun], name: str
) -> None:
    """Deleting an owned consumer is a violation, never a silent pass."""
    run = cache_identity_runs[name]

    assert MUTATIONS[name].expected in run.rule_messages
    assert run.rule_errors == ()


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_every_owner_defect_is_caught_by_the_in_process_rule(
    cache_identity_runs: Mapping[str, MutationRun], name: str
) -> None:
    """Each owner diagnostic and fail-closed case fires through the rule."""
    run = cache_identity_runs[name]
    expected = MUTATIONS[name].expected

    assert any(expected in item for item in run.rule_messages), (
        f"{name}: no rule message contained {expected!r}; got {run.rule_messages}"
    )
    assert run.rule_errors == ()


def test_clean_repository_copy_passes_the_selected_rule(
    cache_identity_runs: Mapping[str, MutationRun],
) -> None:
    """An unmutated copy of this repository reports nothing at all."""
    run = cache_identity_runs["clean"]

    assert run.rule_messages == ()
    assert run.rule_errors == ()


def test_lexical_defense_is_retained_alongside_the_structural_half(
    cache_identity_runs: Mapping[str, MutationRun],
) -> None:
    """Reintroducing the retired cache-URL helper still fails lexically.

    This defect lives in `github_downloader.py`, which the structural half
    never parses, so only the retained legacy greps can catch it.
    """
    run = cache_identity_runs["lexical-retired-cache-url-helper"]

    assert MUTATIONS["lexical-retired-cache-url-helper"].expected in run.rule_messages


# ---------------------------------------------------------------------------
# Static boundary checks on the two modules this change owns.
# ---------------------------------------------------------------------------


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


@pytest.mark.parametrize("relative", OWNED_MODULES)
def test_owned_modules_never_read_parse_or_traverse_on_their_own(relative: str) -> None:
    """The analyzer rides the shared traversal; it never starts its own.

    Parsed rather than grepped so the modules' own prose about `ast.parse`
    and `ast.walk` -- which explains why they are absent -- cannot satisfy
    or trip this check.
    """
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"), filename=relative)

    offenders = sorted(
        {
            name
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            for name in (_dotted(node.func),)
            if name is not None
            and (name in BANNED_CALLEES or name.rsplit(".", 1)[-1] in BANNED_METHODS)
        }
    )
    imported = sorted(
        {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        | {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
    )

    assert offenders == [], f"{relative} performs its own I/O or traversal: {offenders}"
    assert "subprocess" not in imported
    assert "os" not in imported


@pytest.mark.parametrize("relative", OWNED_MODULES)
def test_owned_modules_stay_within_the_module_line_budget(relative: str) -> None:
    """Neither owned module may cross the repository's 1000-line budget."""
    lines = len((ROOT / relative).read_text(encoding="utf-8").splitlines())

    assert lines <= MAX_MODULE_LINES, f"{relative} grew to {lines} lines"


def test_cache_identity_rule_guard_surface_is_unchanged() -> None:
    """Adding the structural half did not change the rule's guard mapping.

    The whole-catalog "these are exactly the 55 registry guards" contract is
    owned by `tests/unit/scripts/test_architecture_runner.py`; this asserts
    only the local invariant this change could have broken.
    """
    rules = {rule.id: rule for rule in registered_rules()}
    rule = rules[RULE_ID]

    assert rule.guard_ids == (RULE_ID,)
    assert rule.group == "transport_platform"
    assert rule.description == (
        "Repository cache-key normalization stays owned by cache/url_normalize.py."
    )
