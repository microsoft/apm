"""Producer-journey remotes for the lifecycle world (P01-P04, #4110).

P01 releases a package: the author clones an empty repository, tags
``v1.0.0`` and runs ``git push --follow-tags``; a consumer then installs
``you/my-skills#v1.0.0``. The world's smart-HTTP server answers fetches
only (no ``git-receive-pack``), so ``publish_producer_twins`` creates the
empty bare repository, registers it with the world (fetches go through the
recording proxy as ``https://github.com/you/my-skills``) and adds a
``pushInsteadOf`` rule to the world's global git config, so the docs'
literal ``git push`` lands in the same bare repository.

``TWINS_PRODUCER`` maps each slug to its (empty) twin; an empty ``files``
mapping means "empty repository, the journey pushes its content".
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path

from tests.lifecycle.journeys.twins import Twin

MY_SKILLS = Twin("you/my-skills", "v1.0.0", {})
TWINS_PRODUCER: Mapping[str, Twin] = {twin.slug: twin for twin in (MY_SKILLS,)}


def publish_producer_twins(services, root: Path, env: Mapping[str, str]) -> dict[str, Path]:
    """Create each producer remote under *root*, empty, and route pushes to it."""
    published: dict[str, Path] = {}
    git_config = env.get("GIT_CONFIG_GLOBAL")
    for slug in TWINS_PRODUCER:
        origin = (root / "repositories" / f"{slug.replace('/', '__')}.git").resolve()
        origin.parent.mkdir(parents=True, exist_ok=True)
        if not origin.exists():
            subprocess.run(
                ("git", "init", "-q", "--bare", "--initial-branch=main", str(origin)),
                env=dict(env),
                check=True,
            )
        if git_config:
            subprocess.run(
                (
                    "git",
                    "config",
                    "--file",
                    git_config,
                    f"url.{origin.as_uri()}.pushInsteadOf",
                    f"https://github.com/{slug}.git",
                ),
                env=dict(env),
                check=True,
            )
        published[slug] = origin
    # Same core gap as publish_twins: WorldServices has no public publish().
    services._repositories.update(published)
    return published
