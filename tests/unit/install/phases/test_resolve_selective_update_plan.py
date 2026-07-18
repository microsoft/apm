"""Selective-update plan facts produced by the resolve phase."""

from types import SimpleNamespace

from apm_cli.install.phases import resolve
from apm_cli.models.dependency.reference import DependencyReference


def test_run_records_complete_keys_before_only_filter(monkeypatch):
    selected = DependencyReference(repo_url="acme/selected")
    unselected = DependencyReference(repo_url="acme/unselected")
    nodes = {
        dep.get_unique_key(): SimpleNamespace(dependency_ref=dep, children=[])
        for dep in (selected, unselected)
    }
    ctx = SimpleNamespace(
        only_packages=["acme/selected"],
        deps_to_install=[],
        dependency_graph=SimpleNamespace(
            dependency_tree=SimpleNamespace(nodes=nodes),
        ),
    )

    for name in (
        "_load_lockfile",
        "_ensure_modules_dir",
        "_setup_downloader",
        "seed_ref_resolver_from_lockfile",
    ):
        monkeypatch.setattr(resolve, name, lambda _ctx: None)
    monkeypatch.setattr(resolve, "resolution_for_context", lambda _ctx: None)
    monkeypatch.setattr(
        resolve,
        "_resolve_dependencies",
        lambda target_ctx, _staging: setattr(
            target_ctx,
            "deps_to_install",
            [selected, unselected],
        ),
    )

    resolve.run(ctx)

    assert ctx.update_plan_complete_dep_keys == {
        selected.get_unique_key(),
        unselected.get_unique_key(),
    }
    assert ctx.deps_to_install == [selected]
    assert ctx.intended_dep_keys == {selected.get_unique_key()}
