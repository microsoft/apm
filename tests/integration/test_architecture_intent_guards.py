"""Integration guardrails for preserving declared dependency intent."""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LINT_SCRIPT = _REPO_ROOT / "scripts" / "lint-architecture-boundaries.sh"


def _find_function(tree: ast.AST, name: str) -> ast.FunctionDef:
    """Locate a (possibly nested/method) function definition by name."""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found")


def _find_call(func: ast.FunctionDef, callee_name: str) -> ast.Call:
    """Locate a direct call to ``callee_name(...)`` inside ``func``'s body."""
    for node in ast.walk(func):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == callee_name
        ):
            return node
    raise AssertionError(f"no call to {callee_name}(...) found inside {func.name}")


def _keyword_value(call: ast.Call, keyword: str) -> ast.expr:
    for kw in call.keywords:
        if kw.arg == keyword:
            return kw.value
    raise AssertionError(f"call to {call.func.id} has no keyword {keyword!r}")  # type: ignore[union-attr]


def _attribute_path(node: ast.AST) -> str | None:
    """Reconstruct a dotted attribute path, e.g. ``self.skill_subset``.

    Returns ``None`` if the node is not a plain ``Name``/``Attribute`` chain.
    """
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return None


def _expression_depends_on(expr: ast.expr, expected_path: str) -> bool:
    """True if any attribute access inside ``expr`` resolves to ``expected_path``."""
    for node in ast.walk(expr):
        if isinstance(node, ast.Attribute) and node.attr == expected_path.rsplit(".", 1)[-1]:
            if _attribute_path(node) == expected_path:
                return True
    return False


def test_locked_dependency_reconstructs_persisted_skill_subset() -> None:
    """LockedDependency.to_dependency_ref() must forward the persisted subset.

    The lockfile is the sole persisted record of a consumer's ``--skill``
    selection. If ``to_dependency_ref()`` stopped forwarding
    ``self.skill_subset`` into the reconstructed ``DependencyReference``
    (e.g. hardcoding ``None`` or a constant), every downstream consumer --
    including audit replay -- would silently lose the narrowing and drift
    would go undetected.
    """
    source_path = _REPO_ROOT / "src" / "apm_cli" / "deps" / "lockfile.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    method = _find_function(tree, "to_dependency_ref")
    call = _find_call(method, "DependencyReference")
    value = _keyword_value(call, "skill_subset")

    assert _expression_depends_on(value, "self.skill_subset"), (
        "to_dependency_ref()'s DependencyReference(...) call must pass "
        "skill_subset derived from self.skill_subset, not a constant"
    )


def test_audit_replay_forwards_locked_skill_subset_without_interpreting_it() -> None:
    """run_replay() must forward, not reinterpret, the locked skill subset.

    ``integrate_package_primitives`` is the canonical owner of skill-subset
    filtering during install/replay. ``run_replay`` must pass through
    ``package_info.dependency_ref.skill_subset`` untouched (no recomputation,
    no dropping to ``None``) so the replay pipeline enforces exactly what was
    locked -- not a value re-derived at replay time.
    """
    source_path = _REPO_ROOT / "src" / "apm_cli" / "install" / "drift.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    func = _find_function(tree, "run_replay")
    call = _find_call(func, "integrate_package_primitives")
    value = _keyword_value(call, "skill_subset")

    assert _expression_depends_on(value, "package_info.dependency_ref.skill_subset"), (
        "run_replay()'s integrate_package_primitives(...) call must pass "
        "skill_subset derived from package_info.dependency_ref.skill_subset"
    )


def test_static_boundary_guard_covers_replay_skill_subset_authority() -> None:
    """The static lint script must guard both propagation edges above.

    A behavioral/AST regression test alone can be deleted or weakened by a
    future change; the architecture-boundary lint script is the second,
    independent guardrail required by the single-canonical-owner discipline
    (see .github/instructions/architecture.instructions.md, AC4).
    """
    lint_source = _LINT_SCRIPT.read_text(encoding="utf-8")
    assert "Audit replay must preserve locked skill subset intent" in lint_source


def test_incompatible_refs_survive_to_conflict_selection(tmp_path: Path) -> None:
    """Two constraints for one package must be reported, not queue-deduped."""
    from apm_cli.deps.apm_resolver import APMDependencyResolver

    (tmp_path / "apm.yml").write_text(
        "\n".join(
            (
                "name: root",
                "version: 1.0.0",
                "dependencies:",
                "  apm:",
                "    - owner/shared#v1",
                "    - owner/shared#v2",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    graph = APMDependencyResolver(max_parallel=1).resolve_dependencies(tmp_path)

    assert graph.has_conflicts()
    conflict = graph.flattened_dependencies.conflicts[0]
    assert conflict.winner.reference == "v1"
    assert [item.reference for item in conflict.conflicts] == ["v2"]


def test_transitive_local_identity_includes_parent_and_anchor(tmp_path: Path) -> None:
    """Equal relative paths from different parents must have distinct identity."""
    from apm_cli.models.dependency.reference import DependencyReference

    first = DependencyReference(
        repo_url="_local/shared",
        is_local=True,
        local_path="../shared",
        source="local",
        declaring_parent="owner/parent-a#main",
        anchored_local_path=str(tmp_path / "a" / "shared"),
    )
    second = DependencyReference(
        repo_url="_local/shared",
        is_local=True,
        local_path="../shared",
        source="local",
        declaring_parent="owner/parent-b#main",
        anchored_local_path=str(tmp_path / "b" / "shared"),
    )
    same_physical = DependencyReference(
        repo_url="_local/shared",
        is_local=True,
        local_path="../shared",
        source="local",
        declaring_parent="owner/parent-c#main",
        anchored_local_path=str(tmp_path / "a" / "shared"),
    )

    assert first.get_unique_key() != second.get_unique_key()
    assert first.get_install_path(tmp_path / "apm_modules") != second.get_install_path(
        tmp_path / "apm_modules"
    )
    assert first.get_unique_key() == same_physical.get_unique_key()
    assert first.get_install_path(tmp_path / "apm_modules") == same_physical.get_install_path(
        tmp_path / "apm_modules"
    )


def test_configured_mcp_registry_url_is_used(monkeypatch) -> None:
    """The URL shown by the command must be the URL passed to its client."""
    from apm_cli.commands import mcp

    captured: list[str | None] = []

    class FakeRegistry:
        def __init__(self, registry_url=None):
            captured.append(registry_url)
            self.client = MagicMock(registry_url=registry_url)

    monkeypatch.delenv(mcp.MCP_REGISTRY_ENV, raising=False)
    monkeypatch.setattr("apm_cli.config.get_mcp_registry_url", lambda: "https://registry.test/v0")
    monkeypatch.setattr("apm_cli.registry.integration.RegistryIntegration", FakeRegistry)

    registry = mcp._build_registry_with_diag(None, MagicMock())

    assert captured == ["https://registry.test/v0"]
    assert registry.client.registry_url == captured[0]


def test_marketplace_registry_routing_returns_registry_dependency(monkeypatch) -> None:
    """Registry intent must reach the package-registry resolver contract."""
    from apm_cli.marketplace.models import (
        MarketplaceManifest,
        MarketplacePlugin,
        MarketplaceSource,
    )
    from apm_cli.marketplace.resolver import resolve_marketplace_plugin

    source = MarketplaceSource(name="catalog", url="https://example.test/catalog.git")
    manifest = MarketplaceManifest(
        name="catalog",
        plugins=(
            MarketplacePlugin(
                name="owner/tool",
                source={"type": "github", "repo": "owner/registry-tool"},
                version="^1.2.0",
                registry="internal",
            ),
        ),
    )
    monkeypatch.setattr(
        "apm_cli.marketplace.resolver.get_marketplace_by_name", lambda _name: source
    )
    monkeypatch.setattr("apm_cli.marketplace.resolver.fetch_or_cache", lambda *_a, **_k: manifest)

    resolution = resolve_marketplace_plugin("owner/tool", "catalog")

    dep = resolution.dependency_reference
    assert dep is not None
    assert dep.source == "registry"
    assert dep.repo_url == "owner/registry-tool"
    assert dep.registry_name == "internal"
    assert dep.reference == "^1.2.0"
