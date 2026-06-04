"""Shared target doubles for skill integrator unit tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock


def attach_skill_deploy_path(target: MagicMock) -> MagicMock:
    """Attach a TargetProfile-like deploy_path implementation to *target*."""

    def _deploy_path(project_root: Path, *parts: str, primitive: str | None = None) -> Path:
        mapping = target.primitives.get(primitive) if primitive is not None else None
        if target.resolved_deploy_root is not None:
            base = target.resolved_deploy_root
        else:
            deploy_root = mapping.deploy_root if mapping is not None else None
            base = project_root / (deploy_root or target.root_dir)
            if mapping is not None and mapping.subdir:
                base = base / mapping.subdir
        return base.joinpath(*parts) if parts else base

    target.deploy_path = _deploy_path
    return target
