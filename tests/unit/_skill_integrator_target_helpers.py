"""Shared target doubles for skill integrator unit tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from apm_cli.integration.targets import TargetProfile


def attach_skill_deploy_path(target: MagicMock) -> MagicMock:
    """Attach the production ``TargetProfile.deploy_path`` method to *target*."""

    def _deploy_path(project_root: Path, *parts: str, primitive: str | None = None) -> Path:
        return TargetProfile.deploy_path(target, project_root, *parts, primitive=primitive)

    target.deploy_path = _deploy_path
    return target
