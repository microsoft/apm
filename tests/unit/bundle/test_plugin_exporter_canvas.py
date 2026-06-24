"""Unit tests: the plugin exporter preserves canvas extensions in the bundle.

Canvas extensions live under ``.apm/extensions/<name>/`` (and, for
plugin-native repos, root ``extensions/``).  The default plugin pack
format must carry them verbatim so an offline bundle can deliver a
canvas; the files stay inert until the consumer enables the ``canvas``
experimental flag and passes ``--trust-canvas-extensions`` at install.
"""

from __future__ import annotations

from pathlib import Path

from apm_cli.bundle.plugin_exporter import (
    _collect_apm_components,
    _collect_root_plugin_components,
)


def test_collect_apm_components_includes_canvas(tmp_path: Path):
    apm = tmp_path / ".apm"
    (apm / "extensions" / "demo").mkdir(parents=True)
    (apm / "agents").mkdir(parents=True)
    (apm / "extensions" / "demo" / "extension.mjs").write_text("export default {};\n")
    (apm / "extensions" / "demo" / "helper.js").write_text("export const h = 1;\n")
    (apm / "agents" / "a1.agent.md").write_text("---\nname: a1\ndescription: d\n---\nb\n")

    rels = sorted(rel for _src, rel in _collect_apm_components(apm))

    assert "extensions/demo/extension.mjs" in rels
    assert "extensions/demo/helper.js" in rels
    assert "agents/a1.agent.md" in rels


def test_collect_root_plugin_components_includes_canvas(tmp_path: Path):
    (tmp_path / "extensions" / "widget").mkdir(parents=True)
    (tmp_path / "extensions" / "widget" / "extension.mjs").write_text("export default {};\n")

    rels = sorted(rel for _src, rel in _collect_root_plugin_components(tmp_path))

    assert "extensions/widget/extension.mjs" in rels


def test_collect_apm_components_no_extensions_dir(tmp_path: Path):
    apm = tmp_path / ".apm"
    (apm / "agents").mkdir(parents=True)
    (apm / "agents" / "a1.agent.md").write_text("---\nname: a1\ndescription: d\n---\nb\n")

    rels = sorted(rel for _src, rel in _collect_apm_components(apm))

    assert not any(r.startswith("extensions/") for r in rels)
