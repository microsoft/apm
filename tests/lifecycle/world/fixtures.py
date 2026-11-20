"""Pytest fixtures of the lifecycle world (#4094).

``lifecycle_template`` builds one template per session, which under xdist is
one per worker; ``lifecycle_world`` clones it per test and clears the egress
log, so every test starts from the same bytes and an empty log.

On Windows the world lives under a short root on the temp drive: pytest's
temp paths (``...\\AppData\\Local\\Temp\\pytest-of-<user>\\pytest-N\\popen-gwN\\``)
plus APM's resolution staging (``apm_modules\\.apm-resolution-staging\\<id>\\
replacements\\<id>\\.git``) exceed Git for Windows' path limit, so Git refuses
APM's config probe with ``fatal: '$GIT_DIR' too big``.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.lifecycle.world.builder import build_template
from tests.lifecycle.world.clone import clone
from tests.lifecycle.world.contract import World

_SHORT_ROOT_ENV = "APM_LIFECYCLE_SHORT_ROOT"


def _short_parent() -> Path | None:
    """A short writable directory for world roots on Windows; None elsewhere."""
    if os.name != "nt":
        return None
    configured = os.environ.get(_SHORT_ROOT_ENV)
    root = Path(configured) if configured else Path(Path(tempfile.gettempdir()).anchor) / "lw"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return root


@pytest.fixture
def _world_dir(tmp_path: Path) -> Iterator[Path]:
    parent = _short_parent()
    if parent is None:
        yield tmp_path / "world"
        return
    base = Path(tempfile.mkdtemp(prefix="w", dir=parent))
    try:
        yield base / "world"
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture(scope="session")
def lifecycle_template(tmp_path_factory: pytest.TempPathFactory) -> Iterator[World]:
    parent = _short_parent()
    base = (
        Path(tempfile.mkdtemp(prefix="t", dir=parent))
        if parent is not None
        else tmp_path_factory.mktemp("lifecycle-template")
    )
    template = build_template(base / "world")
    try:
        yield template
    finally:
        template.services.close()
        if parent is not None:
            shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def lifecycle_world(lifecycle_template: World, _world_dir: Path) -> World:
    world = clone(lifecycle_template, _world_dir)
    world.services.reset()
    return world
