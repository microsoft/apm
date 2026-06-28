"""Round-14 parser break r14-parser-1: stock ``yaml.safe_load`` bypass sinks.

The lifecycle loader (``load_yaml`` in ``utils/yaml_io.py``) carries a merge-
entry budget (round-12) and an alias expansion-weight guard (round-13) so a
billion-laughs apm.yml fails closed as ``yaml.YAMLError`` in milliseconds. But
FIVE manifest-path readers still called stock ``yaml.safe_load`` directly,
BYPASSING the bounded loader entirely:

  * ``install/drift.py::_read_apm_yml_target`` -- reachable from the DEFAULT-ON
    ``apm audit`` drift replay. A merge-key bomb in a committed apm.yml +
    apm.lock.yaml wedged the replay in an O(2^N) CPU hang that the surrounding
    ``except Exception`` cannot catch (a non-terminating loop never raises).
  * ``marketplace/version_check.py::_read_local_version``
  * ``bundle/plugin_exporter.py::_has_marketplace_block``
  * ``commands/init.py::_read_existing_targets``
  * ``commands/marketplace/plugin/__init__.py::_has_marketplace_block``

The fix routes all five through ``load_yaml``; the bomb now raises
``yaml.YAMLError`` (caught by each site's existing guard -> empty/None) instead
of hanging or crashing. These traps build the merge bomb, prove the PRIMARY
drift sink returns FAST (daemon-thread watchdog, no pytest-timeout dependency),
prove each sibling sink fails closed, and prove a LEGIT manifest still parses.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
import yaml

from apm_cli.bundle.plugin_exporter import _has_marketplace_block as exporter_has_mkt
from apm_cli.commands.marketplace.plugin import _has_marketplace_block as plugin_has_mkt
from apm_cli.install.drift import _read_apm_yml_target
from apm_cli.marketplace.version_check import _read_local_version
from apm_cli.utils.yaml_io import load_yaml


def _merge_bomb(levels: int = 40) -> str:
    """A linear-size apm.yml whose merged value-list doubles each level."""
    lines = ["a: &a {k: v}"]
    for i in range(1, levels + 1):
        prev = "a" if i == 1 else f"m{i - 1}"
        lines.append(f"m{i}: &m{i}")
        lines.append(f"  <<: [*{prev}, *{prev}]")
    lines.append(f"target: *m{levels}")
    lines.append("marketplace: *m" + str(levels))
    return "\n".join(lines) + "\n"


def test_bounded_loader_rejects_merge_bomb_fast():
    """The bounded ``load_yaml`` raises YAMLError on the bomb (no hang)."""
    with pytest.raises(yaml.YAMLError):
        load_yaml_str_via_tmp(_merge_bomb())


def load_yaml_str_via_tmp(text: str):
    import tempfile

    p = Path(tempfile.mkdtemp()) / "apm.yml"
    p.write_text(text, encoding="utf-8")
    return load_yaml(p)


def test_drift_read_target_fails_closed_on_bomb(tmp_path):
    """PRIMARY sink: the default-on drift replay must not hang on a merge bomb."""
    (tmp_path / "apm.yml").write_text(_merge_bomb(), encoding="utf-8")
    (tmp_path / "apm.lock.yaml").write_text("apm: {}\n", encoding="utf-8")

    result: dict[str, object] = {}

    def run():
        t0 = time.time()
        result["val"] = _read_apm_yml_target(tmp_path)
        result["dt"] = time.time() - t0

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(15)
    assert not th.is_alive(), "_read_apm_yml_target HUNG >15s on merge bomb (safe_load bypass)"
    assert result["val"] is None, result
    assert result["dt"] < 5.0, f"too slow: {result['dt']}s"


def test_drift_read_target_legit_still_parses(tmp_path):
    """A normal apm.yml target is unaffected by the bounded loader.

    Post #1924 ``_read_apm_yml_target`` routes through the bounded ``load_yaml``
    and ``parse_targets_field``, returning the canonical token LIST (singular or
    plural form) rather than a raw string; the bounded loader does not perturb
    that contract for a benign manifest.
    """
    (tmp_path / "apm.yml").write_text("target: copilot\n", encoding="utf-8")
    assert _read_apm_yml_target(tmp_path) == ["copilot"]
    (tmp_path / "apm.yml").write_text("targets: [copilot, claude]\n", encoding="utf-8")
    assert _read_apm_yml_target(tmp_path) == ["copilot", "claude"]


def test_exporter_marketplace_probe_fails_closed_on_bomb(tmp_path):
    """plugin_exporter._has_marketplace_block fails closed (False) on the bomb."""
    p = tmp_path / "apm.yml"
    p.write_text(_merge_bomb(), encoding="utf-8")
    result: dict[str, object] = {}

    def run():
        result["val"] = exporter_has_mkt(p)

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(15)
    assert not th.is_alive(), "exporter _has_marketplace_block HUNG on merge bomb"
    assert result["val"] is False


def test_plugin_marketplace_probe_fails_closed_on_bomb(tmp_path):
    """commands.marketplace.plugin._has_marketplace_block fails closed on the bomb."""
    p = tmp_path / "apm.yml"
    p.write_text(_merge_bomb(), encoding="utf-8")
    result: dict[str, object] = {}

    def run():
        result["val"] = plugin_has_mkt(p)

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(15)
    assert not th.is_alive(), "plugin _has_marketplace_block HUNG on merge bomb"
    assert result["val"] is False


def test_version_check_fails_closed_on_bomb(tmp_path):
    """version_check._read_local_version returns invalid_yaml (not a hang)."""
    src = tmp_path / "dep"
    src.mkdir()
    (src / "apm.yml").write_text(_merge_bomb(), encoding="utf-8")
    result: dict[str, object] = {}

    def run():
        result["val"] = _read_local_version(tmp_path, "dep")

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(15)
    assert not th.is_alive(), "_read_local_version HUNG on merge bomb"
    version, reason = result["val"]
    assert version is None
    assert reason == "invalid_yaml"


def test_legit_marketplace_block_still_detected(tmp_path):
    """A real marketplace block is still detected through the bounded loader."""
    p = tmp_path / "apm.yml"
    p.write_text("marketplace:\n  name: demo\n", encoding="utf-8")
    assert exporter_has_mkt(p) is True
    assert plugin_has_mkt(p) is True
