"""Tests for registry config merging and default resolution."""

from __future__ import annotations

import apm_cli.config as _conf
from apm_cli.deps.registry.config_loader import resolve_effective_registries


def test_resolve_effective_registries_uses_config_default(monkeypatch):
    monkeypatch.setattr(
        _conf,
        "_config_cache",
        {
            "registries": {
                "corp-main": {
                    "url": "https://corp.example.com/apm",
                    "default": True,
                }
            }
        },
    )
    merged, default = resolve_effective_registries(None, None)
    assert merged == {"corp-main": "https://corp.example.com/apm"}
    assert default == "corp-main"


def test_project_default_overrides_config_default(monkeypatch):
    monkeypatch.setattr(
        _conf,
        "_config_cache",
        {
            "registries": {
                "corp-main": {
                    "url": "https://corp.example.com/apm",
                    "default": True,
                }
            }
        },
    )
    merged, default = resolve_effective_registries(
        {"project-main": "https://project.example.com/apm"},
        "project-main",
    )
    assert merged == {
        "corp-main": "https://corp.example.com/apm",
        "project-main": "https://project.example.com/apm",
    }
    assert default == "project-main"
