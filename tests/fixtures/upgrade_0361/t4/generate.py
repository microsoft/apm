"""Regenerate the T4 upgrade fixtures with an older APM release.

Usage (from the repository root, with an older release's source on PYTHONPATH
through a wrapper executable):

    .venv/bin/python tests/fixtures/upgrade_0361/t4/generate.py <apm-executable> <version>

Writes tests/fixtures/upgrade_0361/t4/<version>/<scenario>/ for every scenario
in ``tests.utils.skill_selection_world.UPGRADE_SCENARIOS``.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from tests.utils.skill_selection_world import (  # noqa: E402
    INSTALL,
    UPGRADE_SCENARIOS,
    SkillSelectionWorld,
    build_upgrade_world,
    copy_capture,
)


def main(apm: str, version: str) -> None:
    out_root = Path(__file__).resolve().parent / version
    for scenario in UPGRADE_SCENARIOS:
        with tempfile.TemporaryDirectory(prefix="t4-upgrade-") as tmp:
            world = SkillSelectionWorld.create(Path(tmp) / "w", (apm,))
            knobs = build_upgrade_world(world, scenario)
            project = world.project(
                "consumer", dependencies=knobs["dependencies"], targets=knobs["targets"]
            )
            with world.serve() as env:
                world.run(INSTALL, cwd=project, env=env, scenario_id=f"gen-{scenario}")
                if "uninstall" in knobs:
                    world.run(
                        ("uninstall", knobs["uninstall"]),
                        cwd=project,
                        env=env,
                        scenario_id=f"gen-{scenario}-uninstall",
                    )
            destination = out_root / scenario
            shutil.rmtree(destination, ignore_errors=True)
            copy_capture(project, destination)
            print(f"wrote {destination}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
