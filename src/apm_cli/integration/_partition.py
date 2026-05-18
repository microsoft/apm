"""Managed-files partitioning helpers for BaseIntegrator.

Extracted from :mod:`apm_cli.integration.base_integrator` to keep
that module under the 500-line ceiling while preserving all behaviour.

``BaseIntegrator`` re-exports these as thin ``@staticmethod`` wrappers
so all call-sites remain unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass

# Backward-compat aliases mapping raw ``{prim}_{target}`` keys to
# the bucket names that existing callers expect.  Shared between
# ``partition_managed_files`` and ``partition_bucket_key`` so the
# mapping is defined exactly once.
_BUCKET_ALIASES: dict = {  # noqa: RUF012
    "prompts_copilot": "prompts",
    "agents_copilot": "agents_github",
    "commands_claude": "commands",
    "commands_cursor": "commands_cursor",
    "commands_opencode": "commands_opencode",
    "instructions_copilot": "instructions",
    "instructions_cursor": "rules_cursor",
    "instructions_claude": "rules_claude",
}


def partition_bucket_key(prim_name: str, target_name: str) -> str:
    """Return the canonical bucket key for a (primitive, target) pair.

    Applies backward-compat aliases so callers stay in sync with
    ``partition_managed_files`` bucket naming.
    """
    raw = f"{prim_name}_{target_name}"
    return _BUCKET_ALIASES.get(raw, raw)


def _build_prefix_trie(prefix_map: dict[str, str]) -> dict[str, dict]:
    """Build a trie for longest-prefix path classification."""
    trie: dict[str, dict] = {}
    for prefix, bucket_key in prefix_map.items():
        segments = [s for s in prefix.split("/") if s]
        node = trie
        for segment in segments:
            child = node.get(segment)
            if child is None:
                child = {}
                node[segment] = child
            node = child
        node["_bucket"] = bucket_key
    return trie


def _classify_path(trie: dict[str, dict], path: str) -> str | None:
    """Return the deepest matching bucket for *path*."""
    segments = [s for s in path.split("/") if s]
    node = trie
    last_bucket: str | None = None
    for segment in segments:
        child = node.get(segment)
        if child is None:
            break
        node = child
        bucket = node.get("_bucket")
        if bucket is not None:
            last_bucket = bucket
    return last_bucket


@dataclass(frozen=True, slots=True)
class _PartitionBuildState:
    """Mutable collections used while building prefix buckets."""

    buckets: dict[str, set[str]]
    prefix_map: dict[str, str]
    skill_prefixes: list[str]
    hook_prefixes: list[str]


def _register_prefix(
    state: _PartitionBuildState,
    target,
    prim_name: str,
    mapping,
) -> None:
    """Register one primitive prefix in the correct bucket collection."""
    if target.resolved_deploy_root is not None:
        if prim_name == "skills":
            from apm_cli.integration.copilot_cowork_paths import COWORK_LOCKFILE_PREFIX

            state.skill_prefixes.append(COWORK_LOCKFILE_PREFIX)
        return
    effective_root = mapping.deploy_root or target.root_dir
    prefix = f"{effective_root}/{mapping.subdir}/" if mapping.subdir else f"{effective_root}/"
    if prim_name == "skills":
        state.skill_prefixes.append(prefix)
        return
    if prim_name == "hooks":
        state.hook_prefixes.append(prefix)
        return
    raw_key = f"{prim_name}_{target.name}"
    bucket_key = _BUCKET_ALIASES.get(raw_key, raw_key)
    state.buckets.setdefault(bucket_key, set())
    state.prefix_map[prefix] = bucket_key


def _categorise_cross_target(
    path: str,
    buckets: dict[str, set[str]],
    skill_prefixes: tuple[str, ...],
    hook_prefixes: tuple[str, ...],
) -> None:
    """Route paths that belong to cross-target buckets."""
    if path.startswith(skill_prefixes):
        buckets["skills"].add(path)
    elif path.startswith(hook_prefixes):
        buckets["hooks"].add(path)


def partition_managed_files(
    managed_files: set[str],
    targets=None,
) -> dict:
    """Partition *managed_files* by integration prefix in a single pass.

    When *targets* is provided, prefixes and bucket keys are derived
    from those (scope-resolved) profiles.  Otherwise falls back to
    ``KNOWN_TARGETS`` for backward compatibility.

    Bucket keys are generated dynamically so adding a new target or
    primitive automatically creates the corresponding bucket.

    Cross-target buckets (``skills``, ``hooks``) group all targets
    together because ``SkillIntegrator`` and ``HookIntegrator``
    handle multi-target sync internally.

    Path routing uses a longest-prefix-match strategy so multi-level
    roots like ``.config/opencode/`` are handled correctly.
    """
    from apm_cli.integration.targets import KNOWN_TARGETS

    source = targets if targets is not None else KNOWN_TARGETS.values()

    buckets: dict = {}

    # Skills and hooks are cross-target (single bucket each)
    skill_prefixes: list = []
    hook_prefixes: list = []

    # prefix -> bucket_key (longest-prefix-match routing)
    prefix_map: dict = {}

    build_state = _PartitionBuildState(buckets, prefix_map, skill_prefixes, hook_prefixes)
    for target in source:
        for prim_name, mapping in target.primitives.items():
            _register_prefix(build_state, target, prim_name, mapping)

    buckets["skills"] = set()
    buckets["hooks"] = set()

    skill_tuple = tuple(skill_prefixes)
    hook_tuple = tuple(hook_prefixes)

    trie = _build_prefix_trie(prefix_map)

    for p in managed_files:
        bucket = _classify_path(trie, p)
        if bucket is not None:
            buckets[bucket].add(p)
            continue
        _categorise_cross_target(p, buckets, skill_tuple, hook_tuple)

    return buckets
