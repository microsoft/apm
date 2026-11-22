"""Unit tests: the plugin exporter preserves canvas extensions in the bundle.

Canvas extensions live under ``.apm/extensions/<name>/`` (and, for
plugin-native repos, root ``extensions/``).  The default plugin pack
format must carry them verbatim so an offline bundle can deliver a
canvas; the files stay inert until the consumer enables the ``canvas``
experimental flag and passes ``--trust-canvas-extensions`` at install.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apm_cli.bundle.plugin_exporter import (
    _cache_would_contribute_primitives,
    _collect_apm_components,
    _collect_root_plugin_components,
    _warn_parked_executables,
)
from apm_cli.deps.lockfile import LockedDependency

pytestmark = pytest.mark.component


def test_collect_apm_components_includes_canvas(tmp_path: Path):
    apm = tmp_path / ".apm"
    (apm / "extensions" / "demo").mkdir(parents=True)
    (apm / "agents").mkdir(parents=True)
    (apm / "extensions" / "demo" / "extension.mjs").write_text("export default {};\n")
    (apm / "extensions" / "demo" / "helper.js").write_text("export const h = 1;\n")
    (apm / "agents" / "a1.agent.md").write_text("---\nname: a1\ndescription: d\n---\nb\n")

    components = list(_collect_apm_components(apm))
    rels = sorted(rel for _src, rel in components)

    assert all(src.is_file() for src, _rel in components)
    assert "extensions/demo/extension.mjs" in rels
    assert "extensions/demo/helper.js" in rels
    assert "agents/a1.agent.md" in rels


def test_collect_root_plugin_components_includes_canvas(tmp_path: Path):
    (tmp_path / "extensions" / "widget").mkdir(parents=True)
    (tmp_path / "extensions" / "widget" / "extension.mjs").write_text("export default {};\n")

    components = list(_collect_root_plugin_components(tmp_path))
    rels = sorted(rel for _src, rel in components)

    assert all(src.is_file() for src, _rel in components)
    assert "extensions/widget/extension.mjs" in rels


def test_collect_apm_components_no_extensions_dir(tmp_path: Path):
    apm = tmp_path / ".apm"
    (apm / "agents").mkdir(parents=True)
    (apm / "agents" / "a1.agent.md").write_text("---\nname: a1\ndescription: d\n---\nb\n")

    components = list(_collect_apm_components(apm))
    rels = sorted(rel for _src, rel in components)

    assert not any(r.startswith("extensions/") for r in rels)
    assert all(src.is_file() for src, _rel in components)


def _canvas_cache(tmp_path: Path) -> Path:
    (tmp_path / ".apm" / "extensions" / "demo").mkdir(parents=True)
    (tmp_path / ".apm" / "extensions" / "demo" / "extension.mjs").write_text("export {};\n")
    return tmp_path


@pytest.mark.parametrize(
    ("exec_status", "allowed", "parked", "remedy"),
    [
        ("gated_pending_approval", (), True, "Run 'apm approve org/canvas' to approve"),
        ("denied", (), True, "Run 'apm policy explain org/canvas' to see which deny"),
        # The lock records the canvas as deployed: no deployed files is a stale install.
        ("gated_pending_approval", ("canvas",), False, None),
        ("deployed", (), False, None),
        (None, (), False, None),
    ],
)
def test_a_gate_parked_canvas_is_omitted_not_a_provenance_gap(
    tmp_path: Path, capfd, exec_status, allowed, parked, remedy
) -> None:
    """#4714: pack leaves out a canvas the lock records as withheld and names its remedy."""
    cache = _canvas_cache(tmp_path)
    dep = LockedDependency(
        repo_url="org/canvas", exec_status=exec_status, exec_allowed_types=allowed
    )

    assert _cache_would_contribute_primitives(cache, dep) is not parked
    _warn_parked_executables(dep, cache)
    printed = " ".join(capfd.readouterr().out.split())

    if not parked:
        assert "not packed" not in printed
        return
    assert "[!] dependency org/canvas: canvas extension(s) not packed" in printed
    assert remedy in printed


def test_a_mixed_denied_row_does_not_claim_the_canvas_is_denied(tmp_path: Path, capfd) -> None:
    """exec_status is the worst case: denied hooks beside a parked canvas (#4714 review)."""
    cache = _canvas_cache(tmp_path)
    hook = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo"}]}]}}
    (cache / ".apm" / "hooks").mkdir(parents=True)
    (cache / ".apm" / "hooks" / "session.json").write_text(json.dumps(hook))
    dep = LockedDependency(repo_url="org/canvas", exec_status="denied")

    _warn_parked_executables(dep, cache)
    printed = " ".join(capfd.readouterr().out.split())

    assert "canvas extension(s) not packed (withheld by the executables gate)" in printed
    assert "(denied)" not in printed
    assert "Run 'apm policy explain org/canvas' to see why and how to allow it" in printed


def test_the_hint_keeps_the_dependency_host_and_names_the_carrying_format(
    tmp_path: Path, capfd
) -> None:
    """The remedy selects the installed dependency; agent-plugin cannot carry canvas."""
    cache = _canvas_cache(tmp_path)
    dep = LockedDependency(
        repo_url="org/canvas", host="git.example.com", exec_status="gated_pending_approval"
    )

    _warn_parked_executables(dep, cache, format_carries=False)
    printed = " ".join(capfd.readouterr().out.split())

    assert "Run 'apm approve git.example.com/org/canvas'" in printed
    assert "pack with '--format claude-plugin' to ship them" in printed
