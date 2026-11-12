"""Root dependency replacement rules: typed model, parser, index, and projection.

A consumer root ``apm.yml`` may declare a ``replace:`` list that redirects an
upstream package identity to a local checkout or a fork throughout the transitive
graph.  The model keeps original identity distinct from the effective source so
every existing consumer (graph, lock, auth, policy, materialization) receives a
clean projection without partial host-patching or split authority.

Canonical owner of:
- ``ReplacementSelector`` coordinate tuple (host, port, repo, subpath)
- ``ReplacementSource`` effective source descriptor
- ``ReplacementRule`` validated binding of selector to source
- ``ReplacementIndex`` O(R+E) lookup by normalized coordinate
- ``parse_replace_block`` root manifest parser
- ``project_effective_reference`` projection from original to effective source

No ref, alias, wildcard, name, registry, or marketplace in the selector.
No rule chaining: rules apply once to original requirements.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from ...cache.url_normalize import SCP_LIKE_RE
from .object_fields import apply_optional_dependency_fields

if TYPE_CHECKING:
    from ..apm_package import APMPackage
    from .reference import DependencyReference


class ReplacementConfigError(ValueError):
    """A replacement rule in ``apm.yml`` is malformed or ambiguous."""


class ReplacementCollisionError(ValueError):
    """An effective replacement source collides with an existing physical slot.

    The existing ``apm_modules/`` layout is host-blind: ``owner/repo`` on
    github.com and ``owner/repo`` on gitlab.com would land in the same
    directory.  Replacement must reject rather than broaden the layout.
    """


class ReplacementCycleError(ValueError):
    """A local replacement path creates a dependency cycle."""


# ---------------------------------------------------------------------------
# Selector: identifies the upstream package to replace
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplacementSelector:
    """Exact normalized coordinate tuple that identifies an upstream package.

    Fields mirror the identity components of ``DependencyReference`` but are
    restricted to explicit supported Git hosts -- no registry, marketplace,
    name-only, or local selectors.

    ``host`` is always lowercase.  ``repo_url`` is normalized through the
    existing ``normalize_package_repo_url`` casing authority.  ``subpath`` is
    normalized to no leading/trailing slashes and forward slashes only.
    ``port`` is ``None`` when the URL uses the default HTTPS port (443).
    """

    host: str
    port: int | None
    repo_url: str
    subpath: str | None

    @property
    def key(self) -> tuple[str, int | None, str, str | None]:
        """Hashable lookup key for the index."""
        return (self.host, self.port, self.repo_url, self.subpath)

    @classmethod
    def from_reference(cls, dep: DependencyReference) -> ReplacementSelector:
        """Use the canonical dependency identity normalization."""
        return cls(
            (dep.host or "github.com").lower(),
            dep.port,
            dep.canonical_repo_url,
            dep.virtual_path if dep.is_virtual else None,
        )


# ---------------------------------------------------------------------------
# Source descriptors: what the upstream identity is redirected to
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LocalReplacementSource:
    """A local filesystem checkout as the effective source.

    ``path`` is the raw string from ``apm.yml`` (e.g. ``../catalog/packages/base``).
    Anchoring to the consumer root happens at bind time, not parse time.
    """

    path: str


@dataclass(frozen=True)
class GitReplacementSource:
    """A Git fork/mirror as the effective source.

    ``ref`` is required -- replacement sources must be explicit about which
    revision they supply.  ``subpath`` is optional (fork may host the package
    at a different location).
    """

    git: str = field(compare=False)
    host: str
    port: int | None
    repo_url: str
    subpath: str | None
    ref: str
    scheme: str | None = None
    ssh_user: str | None = None


# Union of effective sources
ReplacementSource = LocalReplacementSource | GitReplacementSource


# ---------------------------------------------------------------------------
# A validated rule
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplacementRule:
    """One validated replacement binding: ``for`` selector -> ``with`` source."""

    selector: ReplacementSelector
    source: ReplacementSource


@dataclass(frozen=True)
class ReplacementRequest:
    """One original incoming edge, before its complete source/ref substitution."""

    parent: str | None
    ref: str | None
    skills: tuple[str, ...] | None
    targets: tuple[str, ...] | None
    dev: bool
    alias: str | None = None

    def to_dict(self) -> dict:
        """Preserve unset and explicitly empty selections separately."""
        return {
            "parent": self.parent,
            "ref": self.ref,
            "skills": list(self.skills) if self.skills is not None else None,
            "targets": list(self.targets) if self.targets is not None else None,
            "dev": self.dev,
            "alias": self.alias,
        }


@dataclass
class ReplacementBinding:
    """Original ownership and main-thread edge evidence for one selected source."""

    original: DependencyReference
    rule: ReplacementRule
    consumer_root: Path | None = None
    requests: dict[ReplacementRequest, None] = field(default_factory=dict)

    def record(self, dep: DependencyReference, parent: str | None, is_dev: bool) -> None:
        """Record every incoming edge in O(1), including deduplicated diamonds."""
        request = ReplacementRequest(
            parent,
            dep.reference,
            tuple(dep.skill_subset) if dep.skill_subset is not None else None,
            tuple(dep.target_subset) if dep.target_subset is not None else None,
            is_dev,
            alias=dep.alias,
        )
        self.requests[request] = None

    def to_dict(self) -> dict:
        """Serialize selected-source evidence independently of logical identity."""
        return {
            "source": replacement_rule_to_dict(self.rule)["with"],
            "requests": [request.to_dict() for request in self.requests],
        }

    def retain_parents(self, parent_keys: set[str]) -> bool:
        """Prune removed dependency requests without changing chosen ownership."""
        retained = {
            request: None
            for request in self.requests
            if request.parent is None or request.parent in parent_keys
        }
        if not retained:
            raise ReplacementConfigError(
                f"Replacement {self.original.get_unique_key()} has no surviving original request"
            )
        changed = retained != self.requests
        self.requests = retained
        return changed

    @classmethod
    def from_dict(cls, raw: object, original: DependencyReference) -> ReplacementBinding:
        """Validate lock evidence without accessing the selected source."""
        if not isinstance(raw, dict) or set(raw) != {"source", "requests"}:
            raise ReplacementConfigError("Replacement evidence requires source and requests")
        if original.is_local or original.source == "registry" or original.artifactory_prefix:
            raise ReplacementConfigError("Replacement evidence must belong to a Git identity")
        source = _parse_source(raw["source"], rule_index=0)
        requests = raw["requests"]
        if not isinstance(requests, list) or not requests:
            raise ReplacementConfigError("Replacement evidence requires original incoming requests")
        binding = cls(
            original, ReplacementRule(ReplacementSelector.from_reference(original), source)
        )
        for request in requests:
            if not isinstance(request, dict) or set(request) != {
                "parent",
                "ref",
                "skills",
                "targets",
                "dev",
                "alias",
            }:
                raise ReplacementConfigError("Malformed replacement request evidence")
            if any(
                request[key] is not None and (not isinstance(request[key], str) or not request[key])
                for key in ("parent", "ref")
            ) or not isinstance(request["dev"], bool):
                raise ReplacementConfigError("Invalid replacement parent, ref, or dev intent")
            edge = replace(
                original,
                reference=request["ref"],
                alias=None,
                skill_subset=None,
                target_subset=None,
            )
            try:
                # Persisted normalized selections may be explicitly contracted
                # to empty; that must not expand back to the default "all".
                selections = {key: request[key] for key in ("alias", "skills", "targets")}
                for key, attribute in (("skills", "skill_subset"), ("targets", "target_subset")):
                    if selections[key] == []:
                        setattr(edge, attribute, [])
                        del selections[key]
                apply_optional_dependency_fields(edge, selections)
            except ValueError as exc:
                raise ReplacementConfigError(f"Invalid replacement selection: {exc}") from exc
            if not binding.requests:
                binding.original = replace(edge, alias=original.alias)
            binding.record(edge, request["parent"], request["dev"])
        return binding

    def local_source_path(self, consumer_root: Path | None = None) -> Path:
        """Anchor a root-owned local source independently of the incoming parent."""
        if not isinstance(self.rule.source, LocalReplacementSource):
            raise ValueError("Replacement source is not local")
        root = consumer_root or self.consumer_root
        path = Path(self.rule.source.path).expanduser()
        if not path.is_absolute():
            if root is None:
                raise ValueError("Local replacement requires the consumer manifest directory")
            path = root / path
        return path.resolve()


# ---------------------------------------------------------------------------
# Index: O(R+E) lookup
# ---------------------------------------------------------------------------


class ReplacementIndex:
    """O(1) lookup of replacement rules by normalized coordinate tuple.

    Built once from the parsed ``replace:`` list.  The index rejects duplicate
    selectors at construction time so graph binding is unambiguous.
    """

    def __init__(self, rules: list[ReplacementRule]) -> None:
        self._rules = list(rules)
        self._by_key: dict[tuple[str, int | None, str, str | None], ReplacementRule] = {}
        self._bindings: dict[tuple[str, int | None, str, str | None], ReplacementBinding] = {}
        self._slots: dict[str, str] = {}
        self._identities: dict[str, str] = {}
        for rule in self._rules:
            key = rule.selector.key
            if key in self._by_key:
                raise ReplacementConfigError(
                    f"Duplicate replacement selector: "
                    f"host={rule.selector.host!r} "
                    f"repo={rule.selector.repo_url!r} "
                    f"path={rule.selector.subpath!r} "
                    f"collides with an earlier rule for the same coordinates"
                )
            self._by_key[key] = rule

    def lookup(self, dep: DependencyReference) -> ReplacementRule | None:
        """Return the matching rule for *dep*, or ``None``.

        Matches on the original identity coordinates of the dependency:
        host (lowercase), port, normalized repo_url, and virtual_path (subpath).
        Local, registry, marketplace, and parent-inheritance deps never match.
        """
        if (
            dep.replacement is not None
            or dep.is_local
            or dep.source == "registry"
            or dep.is_marketplace
        ):
            return None
        if dep.is_parent_repo_inheritance:
            return None
        return self._by_key.get(ReplacementSelector.from_reference(dep).key)

    def bind(
        self,
        dep: DependencyReference,
        *,
        consumer_root: Path,
        modules_dir: Path,
        parent: str | None,
        is_dev: bool,
    ) -> DependencyReference:
        """Apply the consumer's table once, before graph deduplication/admission."""
        if not self._rules or dep.replacement is not None:
            return dep
        identity = dep.get_identity()
        slot = dep.get_install_path(modules_dir)
        key = dep.get_unique_key()
        for occupied, entry in ((self._slots, slot.as_posix()), (self._identities, key)):
            previous = occupied.setdefault(entry, identity)
            if previous != identity:
                raise ReplacementCollisionError(
                    f"Replacement graph identities {previous!r} and {identity!r} "
                    f"share the existing materialization slot {slot}. "
                    "Use distinct original package identities and destinations."
                )
        rule = self.lookup(dep)
        if rule is None:
            return dep
        binding = self._bindings.get(rule.selector.key)
        if binding is None:
            binding = ReplacementBinding(dep, rule, consumer_root)
            self._bindings[rule.selector.key] = binding
        binding.record(dep, parent, is_dev)
        effective = project_effective_reference(dep, rule)
        effective.replacement = binding
        if effective.is_local:
            source = binding.local_source_path()
            if source == consumer_root.resolve():
                raise ReplacementCycleError("Local replacement cycle: source is the consumer root")
            effective.anchored_local_path = source.as_posix()
        return effective

    @property
    def unused_rules(self) -> list[ReplacementRule]:
        """Root rules that matched no incoming edge in this resolution."""
        return [rule for rule in self._rules if rule.selector.key not in self._bindings]

    @property
    def rules(self) -> list[ReplacementRule]:
        """All rules in declaration order."""
        return list(self._rules)

    def __len__(self) -> int:
        return len(self._rules)

    def __bool__(self) -> bool:
        return bool(self._rules)

    @staticmethod
    def empty() -> ReplacementIndex:
        """Return an empty index (no replacements configured)."""
        return ReplacementIndex([])


# ---------------------------------------------------------------------------
# Parser: ``replace:`` block in root apm.yml
# ---------------------------------------------------------------------------

_SELECTOR_ALLOWED_KEYS = frozenset({"git", "path"})
_LOCAL_SOURCE_ALLOWED_KEYS = frozenset({"path"})
_GIT_SOURCE_ALLOWED_KEYS = frozenset({"git", "path", "ref"})
_RULE_ALLOWED_KEYS = frozenset({"for", "with"})


def _parse_git_entry(
    raw: dict,
    *,
    context: str,
) -> DependencyReference:
    """Delegate provider, endpoint, subpath and transport parsing to its owner."""
    from .reference import DependencyReference

    git = raw["git"].strip()
    parsed = urllib.parse.urlparse(git)
    scp = SCP_LIKE_RE.match(git)
    if not scp and parsed.scheme not in {"https", "http", "ssh"}:
        raise ReplacementConfigError(f"{context}: 'git' must be an explicit supported Git URL")
    if (
        "?" in git
        or "#" in git
        or (parsed.scheme in {"https", "http"} and (parsed.username or parsed.password))
    ):
        raise ReplacementConfigError(
            f"{context}: Git URL must not contain credentials, query, or ref"
        )
    try:
        dep = DependencyReference.parse_from_dict(raw)
        if dep.is_local or dep.artifactory_prefix or dep.is_parent_repo_inheritance:
            raise ValueError("expected a supported Git repository")
        if dep.is_virtual and not dep.is_virtual_subdirectory():
            raise ValueError("replacement path must select a package directory, not a primitive")
    except ValueError as exc:
        raise ReplacementConfigError(f"{context}: {exc}") from exc
    return dep


def _parse_selector(raw: object, *, rule_index: int) -> ReplacementSelector:
    """Parse the ``for`` block of one replacement rule."""
    context = f"replace[{rule_index}].for"
    if not isinstance(raw, dict):
        raise ReplacementConfigError(f"{context}: expected a mapping, got {type(raw).__name__}")
    unknown = set(raw.keys()) - _SELECTOR_ALLOWED_KEYS
    if unknown:
        raise ReplacementConfigError(
            f"{context}: unknown field(s) {sorted(unknown)}. "
            f"Allowed fields: {sorted(_SELECTOR_ALLOWED_KEYS)}"
        )
    git_url = raw.get("git")
    if git_url is None:
        raise ReplacementConfigError(f"{context}: 'git' field is required")
    if not isinstance(git_url, str) or not git_url.strip():
        raise ReplacementConfigError(f"{context}: 'git' must be a non-empty string")

    dep = _parse_git_entry(raw, context=context)
    return ReplacementSelector.from_reference(dep)


def _parse_local_source(raw: dict, *, rule_index: int) -> LocalReplacementSource:
    """Parse a local-path ``with`` block."""
    context = f"replace[{rule_index}].with"
    path_val = raw["path"]
    if not isinstance(path_val, str) or not path_val.strip():
        raise ReplacementConfigError(f"{context}: 'path' must be a non-empty string")
    path_str = path_val.strip()

    # Validate that it looks like a local path (reuse DependencyReference check)
    from .reference import DependencyReference

    if not DependencyReference.is_local_path(path_str):
        raise ReplacementConfigError(
            f"{context}: local 'path' must start with './', '../', '/', or '~/' (got {path_str!r})"
        )
    return LocalReplacementSource(path=path_str)


def _parse_git_source(raw: dict, *, rule_index: int) -> GitReplacementSource:
    """Parse a Git fork ``with`` block."""
    context = f"replace[{rule_index}].with"
    git_url = raw["git"]
    if not isinstance(git_url, str) or not git_url.strip():
        raise ReplacementConfigError(f"{context}: 'git' must be a non-empty string")

    ref_val = raw.get("ref")
    if ref_val is None:
        raise ReplacementConfigError(
            f"{context}: Git replacement source requires an explicit 'ref' field"
        )
    if not isinstance(ref_val, str) or not ref_val.strip():
        raise ReplacementConfigError(f"{context}: 'ref' must be a non-empty string")

    dep = _parse_git_entry(raw, context=context)
    if dep.is_insecure:
        raise ReplacementConfigError(
            f"{context}: HTTP replacement sources are not supported; use HTTPS or SSH"
        )
    return GitReplacementSource(
        git=git_url.strip(),
        host=(dep.host or "github.com").lower(),
        port=dep.port,
        repo_url=dep.canonical_repo_url,
        subpath=dep.virtual_path,
        ref=ref_val.strip(),
        scheme=dep.explicit_scheme,
        ssh_user=dep.ssh_user,
    )


def _parse_source(raw: object, *, rule_index: int) -> ReplacementSource:
    """Parse the ``with`` block of one replacement rule."""
    context = f"replace[{rule_index}].with"
    if not isinstance(raw, dict):
        raise ReplacementConfigError(f"{context}: expected a mapping, got {type(raw).__name__}")

    has_git = "git" in raw
    has_path = "path" in raw

    if has_git and has_path and "ref" not in raw:
        # git + path without ref: ambiguous (could be local or git with subpath)
        raise ReplacementConfigError(
            f"{context}: Git replacement source requires an explicit 'ref' field"
        )

    if has_git:
        allowed = _GIT_SOURCE_ALLOWED_KEYS
        unknown = set(raw.keys()) - allowed
        if unknown:
            raise ReplacementConfigError(
                f"{context}: unknown field(s) {sorted(unknown)}. "
                f"Allowed fields for Git source: {sorted(allowed)}"
            )
        return _parse_git_source(raw, rule_index=rule_index)

    if has_path:
        unknown = set(raw.keys()) - _LOCAL_SOURCE_ALLOWED_KEYS
        if unknown:
            raise ReplacementConfigError(
                f"{context}: unknown field(s) {sorted(unknown)}. "
                f"Allowed fields for local source: {sorted(_LOCAL_SOURCE_ALLOWED_KEYS)}"
            )
        return _parse_local_source(raw, rule_index=rule_index)

    raise ReplacementConfigError(
        f"{context}: must contain either 'path' (local) or 'git' + 'ref' (Git fork)"
    )


def parse_replace_block(raw: object) -> list[ReplacementRule]:
    """Parse the top-level ``replace:`` list from a root ``apm.yml``.

    Returns a list of validated rules.  Raises ``ReplacementConfigError``
    for any structural, type, or semantic violation.

    Dependency manifests can be parsed, but only the consumer root's table
    is applied by the resolver.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ReplacementConfigError(f"'replace' must be a list, got {type(raw).__name__}")
    rules: list[ReplacementRule] = []
    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ReplacementConfigError(
                f"replace[{idx}]: each entry must be a mapping with 'for' and 'with' keys, "
                f"got {type(entry).__name__}"
            )
        unknown = set(entry.keys()) - _RULE_ALLOWED_KEYS
        if unknown:
            raise ReplacementConfigError(
                f"replace[{idx}]: unknown field(s) {sorted(unknown)}. "
                f"Allowed fields: {sorted(_RULE_ALLOWED_KEYS)}"
            )
        if "for" not in entry:
            raise ReplacementConfigError(f"replace[{idx}]: missing required 'for' field")
        if "with" not in entry:
            raise ReplacementConfigError(f"replace[{idx}]: missing required 'with' field")

        selector = _parse_selector(entry["for"], rule_index=idx)
        source = _parse_source(entry["with"], rule_index=idx)
        rules.append(ReplacementRule(selector=selector, source=source))
    return rules


def build_replacement_index(rules: list[ReplacementRule]) -> ReplacementIndex:
    """Build a lookup index from parsed rules; rejects duplicate selectors."""
    return ReplacementIndex(rules)


def project_manifest_dependencies(
    manifest: APMPackage, *, modules_dir: Path | None = None
) -> list[DependencyReference]:
    """Project direct root declarations without changing manifest ownership."""
    groups = (
        (False, manifest.get_apm_dependencies()),
        (True, manifest.get_dev_apm_dependencies()),
    )
    if not manifest.replace_rules:
        return [dep for _, dependencies in groups for dep in dependencies]
    root = Path(manifest.source_path or manifest.package_path).resolve()
    index = build_replacement_index(manifest.replace_rules)
    return [
        index.bind(
            dep,
            consumer_root=root,
            modules_dir=modules_dir or root / "apm_modules",
            parent=None,
            is_dev=is_dev,
        )
        for is_dev, dependencies in groups
        for dep in dependencies
    ]


# ---------------------------------------------------------------------------
# Projection: original DependencyReference -> effective source
# ---------------------------------------------------------------------------


def project_effective_reference(
    dep: DependencyReference,
    rule: ReplacementRule,
) -> DependencyReference:
    """Return a new ``DependencyReference`` reflecting the effective source.

    The returned reference carries the replacement source coordinates while the
    caller retains the original ``dep`` for identity/provenance.  The projection
    preserves the original's alias, subset selections, and dev intent.

    For a local source, the effective reference is a local-path dependency.
    For a Git source, the effective reference carries the fork's coordinates
    and the required ref, with the original virtual_path replaced by the
    source's subpath when specified.
    """
    from .reference import DependencyReference as DepRef

    effective = DepRef.parse_from_dict(replacement_rule_to_dict(rule)["with"])
    return replace(
        effective,
        alias=dep.alias,
        skill_subset=list(dep.skill_subset) if dep.skill_subset is not None else None,
        target_subset=list(dep.target_subset) if dep.target_subset is not None else None,
    )


# ---------------------------------------------------------------------------
# Lock-file serialization helpers
# ---------------------------------------------------------------------------


def replacement_rule_to_dict(rule: ReplacementRule) -> dict:
    """Serialize a rule for lockfile ``replacements:`` list."""
    from .reference import DependencyReference

    selector_dict: dict[str, object] = {}
    sel = rule.selector
    identity = DependencyReference(
        repo_url=sel.repo_url, host=sel.host, port=sel.port
    ).with_derived_provider_coordinates()
    url = identity.to_github_url()
    selector_dict["git"] = url if identity.is_azure_devops() else f"{url}.git"
    if sel.subpath:
        selector_dict["path"] = sel.subpath

    source_dict: dict[str, object] = {}
    src = rule.source
    if isinstance(src, LocalReplacementSource):
        source_dict["path"] = src.path
    elif isinstance(src, GitReplacementSource):
        source_dict["git"] = src.git
        if src.subpath:
            source_dict["path"] = src.subpath
        source_dict["ref"] = src.ref

    return {"for": selector_dict, "with": source_dict}


def replacement_rules_from_dicts(raw_list: list[dict]) -> list[ReplacementRule]:
    """Deserialize rules from a lockfile ``replacements:`` list.

    Reuses ``parse_replace_block`` for validation so lock-file rules are
    subject to the same structural checks as manifest rules.
    """
    return parse_replace_block(raw_list)
