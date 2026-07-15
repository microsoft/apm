"""Immutable records for declarative lifecycle scenarios."""

from __future__ import annotations as _annotations

from dataclasses import dataclass as _dataclass
from pathlib import Path as _Path

from tests.utils.apm_lifecycle_runner import CommandResult as _CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshot as _ArtifactSnapshot


@_dataclass(frozen=True)
class LifecycleAction:
    """Describe one lifecycle command and its expected return code."""

    args: tuple[str, ...]
    expected_returncode: int = 0


@_dataclass(frozen=True)
class ScenarioObservation:
    """Collect source inputs, command results, and artifact snapshots."""

    source_inputs: tuple[_Path, ...]
    results: tuple[_CommandResult, ...]
    snapshots: tuple[_ArtifactSnapshot, ...]


@_dataclass(frozen=True)
class ScenarioRow:
    """Compose source inputs and expected lifecycle actions."""

    id: str
    source_inputs: tuple[_Path, ...]
    lifecycle_actions: tuple[LifecycleAction, ...]
