"""Tests for the ``apm pack --check-clean`` drift gate (Wave 4)."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from apm_cli.marketplace.builder import BuildOptions, MarketplaceBuilder
from apm_cli.marketplace.drift_check import (
    DriftDifference,
    DriftOutputReport,
    check_marketplace_drift,
    json_key_diff,
    render_diff_lines,
)
from apm_cli.marketplace.migration import load_marketplace_config

_APM_LOCAL_ONLY = """\
name: my-project
description: A project.
version: 1.0.0
marketplace:
  owner:
    name: ACME
  packages:
    - name: local-tool
      source: ./packages/local-tool
      description: A locally vendored tool.
      version: 0.1.0
"""

_APM_WITH_TWO_OUTPUTS = """\
name: my-project
description: A project.
version: 1.0.0
marketplace:
  owner:
    name: ACME
  outputs:
    claude: {}
    codex: {}
  packages:
    - name: local-tool
      source: ./packages/local-tool
      description: A locally vendored tool.
      version: 0.1.0
      category: tools
"""


def _write(p: Path, content: str) -> None:
    p.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


def _setup_project(tmp_path: Path, apm_yml: str = _APM_LOCAL_ONLY) -> Path:
    _write(tmp_path / "apm.yml", apm_yml)
    return tmp_path


def _make_builder(project_root: Path) -> MarketplaceBuilder:
    config = load_marketplace_config(project_root)
    return MarketplaceBuilder.from_config(
        config, project_root=project_root, options=BuildOptions(dry_run=True, offline=True)
    )


def _write_current_marketplace_json(project_root: Path, *, output_name: str = "claude") -> Path:
    """Write the canonical current document so that drift = unchanged."""
    config = load_marketplace_config(project_root)
    builder = _make_builder(project_root)
    resolved = builder.resolve().entries
    from apm_cli.marketplace.output_profiles import MARKETPLACE_OUTPUTS

    profile = MARKETPLACE_OUTPUTS[output_name]
    doc, _w, _d = builder.compose_output(
        profile, resolved, remote_metadata=builder.remote_metadata_for_profile(profile, resolved)
    )
    payload = MarketplaceBuilder._serialize_json(doc)
    # Resolve the on-disk path the gate compares against.
    rel_path = None
    for spec in config.output_specs:
        if spec.name == output_name:
            rel_path = spec.path
            break
    if rel_path is None:
        rel_path = profile.default_output
    path = project_root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# json_key_diff helper
# ---------------------------------------------------------------------------


class TestJsonKeyDiff:
    def test_identical_returns_empty(self):
        a = {"x": 1, "y": [1, 2]}
        b = {"x": 1, "y": [1, 2]}
        assert json_key_diff(a, b) == []

    def test_leaf_change_detected(self):
        a = {"x": 1}
        b = {"x": 2}
        diffs = json_key_diff(a, b)
        assert len(diffs) == 1
        assert diffs[0].path == "x"
        assert diffs[0].old == 1
        assert diffs[0].new == 2

    def test_nested_path_format(self):
        a = {"plugins": [{"source": {"sha": "aaa"}}]}
        b = {"plugins": [{"source": {"sha": "bbb"}}]}
        diffs = json_key_diff(a, b)
        assert any(d.path == "plugins[0].source.sha" for d in diffs)


# ---------------------------------------------------------------------------
# Clean (unchanged) case
# ---------------------------------------------------------------------------


class TestDriftCleanCase:
    def test_unchanged_when_on_disk_matches(self, tmp_path: Path):
        project_root = _setup_project(tmp_path)
        _write_current_marketplace_json(project_root)
        builder = _make_builder(project_root)
        config = load_marketplace_config(project_root)
        report = check_marketplace_drift(builder, config, project_root)
        assert report.ok
        assert len(report.outputs) == 1
        assert report.outputs[0].status == "unchanged"
        assert report.outputs[0].differences == ()
        assert report.outputs[0].format == "claude"

    def test_ok_report_payload(self, tmp_path: Path):
        project_root = _setup_project(tmp_path)
        _write_current_marketplace_json(project_root)
        builder = _make_builder(project_root)
        config = load_marketplace_config(project_root)
        report = check_marketplace_drift(builder, config, project_root)
        payload = report.to_json_dict()
        assert payload["ok"] is True
        assert payload["outputs"][0]["status"] == "unchanged"
        assert payload["outputs"][0]["differences"] == []


# ---------------------------------------------------------------------------
# Dirty (drift) case
# ---------------------------------------------------------------------------


class TestDriftDirtyCase:
    def test_drift_detected_when_on_disk_differs(self, tmp_path: Path):
        project_root = _setup_project(tmp_path)
        out_path = _write_current_marketplace_json(project_root)
        # Mutate on-disk doc to introduce drift.
        on_disk = json.loads(out_path.read_text(encoding="utf-8"))
        on_disk["plugins"][0]["version"] = "9.9.9"
        out_path.write_text(json.dumps(on_disk, indent=2), encoding="utf-8")
        builder = _make_builder(project_root)
        config = load_marketplace_config(project_root)
        report = check_marketplace_drift(builder, config, project_root)
        assert not report.ok
        assert report.outputs[0].status == "drift"
        assert len(report.outputs[0].differences) >= 1
        paths = [d.path for d in report.outputs[0].differences]
        assert any("plugins[0].version" in p for p in paths)

    def test_error_message_emitted(self, tmp_path: Path):
        project_root = _setup_project(tmp_path)
        out_path = _write_current_marketplace_json(project_root)
        on_disk = json.loads(out_path.read_text(encoding="utf-8"))
        on_disk["plugins"][0]["version"] = "9.9.9"
        out_path.write_text(json.dumps(on_disk, indent=2), encoding="utf-8")
        builder = _make_builder(project_root)
        config = load_marketplace_config(project_root)
        report = check_marketplace_drift(builder, config, project_root)
        msgs = report.error_messages()
        assert len(msgs) == 1
        assert "marketplace.json" in msgs[0]

    def test_render_diff_lines_caps_output(self):
        # Build a report with > 20 diffs to test cap.
        diffs = tuple(DriftDifference(path=f"k{i}", old=i, new=i + 1) for i in range(25))
        out = DriftOutputReport(
            format="claude", path="marketplace.json", status="drift", differences=diffs
        )
        lines = render_diff_lines(out)
        # 20 visible + 1 footer line ("...N more...")
        assert len(lines) <= 21


# ---------------------------------------------------------------------------
# Missing case
# ---------------------------------------------------------------------------


class TestDriftMissingCase:
    def test_missing_when_no_on_disk_file(self, tmp_path: Path):
        project_root = _setup_project(tmp_path)
        # Do NOT write marketplace.json
        builder = _make_builder(project_root)
        config = load_marketplace_config(project_root)
        report = check_marketplace_drift(builder, config, project_root)
        assert not report.ok
        assert report.outputs[0].status == "missing"
        # Missing case: all diffs have old=None.
        for d in report.outputs[0].differences:
            assert d.old is None

    def test_missing_error_message(self, tmp_path: Path):
        project_root = _setup_project(tmp_path)
        builder = _make_builder(project_root)
        config = load_marketplace_config(project_root)
        report = check_marketplace_drift(builder, config, project_root)
        msgs = report.error_messages()
        assert len(msgs) == 1
        assert "missing" in msgs[0]


# ---------------------------------------------------------------------------
# Mixed outputs
# ---------------------------------------------------------------------------


class TestDriftMixedOutputs:
    def test_per_output_status_independent(self, tmp_path: Path):
        project_root = _setup_project(tmp_path, apm_yml=_APM_WITH_TWO_OUTPUTS)
        # Write only the claude output; codex remains missing.
        _write_current_marketplace_json(project_root, output_name="claude")
        builder = _make_builder(project_root)
        config = load_marketplace_config(project_root)
        report = check_marketplace_drift(builder, config, project_root)
        assert not report.ok
        # Outputs sorted/iterated in config order; one should be unchanged, one missing.
        statuses = {o.format: o.status for o in report.outputs}
        assert statuses.get("claude") == "unchanged"
        assert statuses.get("codex") == "missing"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
