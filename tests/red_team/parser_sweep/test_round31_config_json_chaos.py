"""Round-31 PARSER/CHAOS -- ~/.apm/config.json is an UNBOUNDED stdlib-json parse.

``apm install`` resolves the effective registry set via
``deps/registry/config_loader.load_merged_registries`` ->
``_load_config_json_registries`` -> ``config._get_registries_section`` ->
``config.get_config`` -> ``json.load(open(~/.apm/config.json))``.

Unlike the apm.yml family (bounded loader) and the round-30 registry YAML
fix, this JSON read has NO size/depth bound and NO surrounding guard:
``_load_config_json_registries`` calls it bare, and ``load_merged_registries``
does ``merged.update(_load_config_json_registries())`` with no try/except.

A pathological ``config.json`` (deeply-nested arrays -> ``RecursionError``,
or a >4300-digit integer -> ``ValueError``) therefore crashes the registry
merge of a default ``apm install``. Lower severity than the plugin-package
vectors because ``config.json`` is a local user file rather than content
shipped inside a third-party package, but it is still an unbounded,
unguarded parser on the install path -- defense-in-depth gap of the same
class the round-30 fix closed for registry YAML.

Tests assert the SECURE behavior (fail closed, fast). RED on HEAD.
"""

from __future__ import annotations

import time

from .conftest import run_guarded

_DEEP = "[" * 60000 + "]" * 60000
_HUGE_INT = '{"registries": {"r": {"url": ' + "1" + "0" * 4400 + "}}}"


def _wire_config(monkeypatch, tmp_path, text: str):
    """Point the real config loader at a hermetic temp config.json."""
    import apm_cli.config as cfg

    p = tmp_path / "config.json"
    p.write_text(text, encoding="utf-8")
    monkeypatch.setattr(cfg, "CONFIG_FILE", str(p))
    monkeypatch.setattr(cfg, "_config_cache", None)


def test_config_json_deep_nest_does_not_crash_registry_merge(monkeypatch, tmp_path):
    """A deep-nested config.json must fail closed, not raise RecursionError."""
    from apm_cli.deps.registry import config_loader

    _wire_config(monkeypatch, tmp_path, _DEEP)

    start = time.monotonic()
    finished, _result, exc = run_guarded(config_loader.load_merged_registries, timeout=3.0)
    elapsed = time.monotonic() - start

    assert finished, "registry merge hung on deep-nested config.json"
    assert elapsed < 3.0, f"parse took too long: {elapsed:.2f}s"
    assert exc is None, f"install registry merge crashed on config.json: {exc!r}"


def test_config_json_huge_int_does_not_crash_registry_merge(monkeypatch, tmp_path):
    """A >4300-digit int in config.json must fail closed, not raise ValueError."""
    from apm_cli.deps.registry import config_loader

    _wire_config(monkeypatch, tmp_path, _HUGE_INT)

    finished, _result, exc = run_guarded(config_loader.load_merged_registries, timeout=3.0)

    assert finished, "registry merge hung on huge-int config.json"
    assert exc is None, f"install registry merge crashed on config.json: {exc!r}"


def test_benign_config_json_still_merges(monkeypatch, tmp_path):
    """A well-formed config.json must still resolve registries."""
    from apm_cli.deps.registry import config_loader

    _wire_config(
        monkeypatch,
        tmp_path,
        '{"registries": {"corp": {"url": "https://reg.example.com"}}}',
    )

    finished, result, exc = run_guarded(config_loader.load_merged_registries, timeout=3.0)

    assert finished and exc is None, f"benign config.json failed: {exc!r}"
    assert result.get("corp") == "https://reg.example.com"
