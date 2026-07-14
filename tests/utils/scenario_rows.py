"""Immutable records for declarative lifecycle scenarios."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypeAlias

if TYPE_CHECKING:
    from tests.utils.apm_lifecycle_runner import CommandResult
    from tests.utils.artifact_snapshot import ArtifactSnapshot


@dataclass(frozen=True)
class LifecycleAction:
    """Describe one lifecycle command and its expected return code."""

    args: tuple[str, ...]
    expected_returncode: int = 0


@dataclass(frozen=True)
class ScenarioObservation:
    """Collect source inputs, command results, and artifact snapshots."""

    source_inputs: tuple[Path, ...]
    results: tuple[CommandResult, ...]
    snapshots: tuple[ArtifactSnapshot, ...]


ScenarioAssertion: TypeAlias = Callable[[ScenarioObservation], None]


@dataclass(frozen=True)
class ScenarioRow:
    """Compose source inputs, lifecycle actions, and scenario assertions."""

    id: str
    source_inputs: tuple[Path, ...]
    lifecycle_actions: tuple[LifecycleAction, ...]
    assertions: tuple[ScenarioAssertion, ...]
