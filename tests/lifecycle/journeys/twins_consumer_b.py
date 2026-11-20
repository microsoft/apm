"""Content twins for the consumer_b journeys (J10-J15) (#4110).

J10-J15 install the world's own packages (``lifecycle/p-skill#v1.0.0``) and
bundles they pack in setup. The one upstream they reach is the repository
``apm doctor`` probes for network reachability (``git ls-remote
https://github.com/git/git.git HEAD``): J14 publishes a twin of it so the
check runs against the world's local git server.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from tests.lifecycle.journeys.twins import Twin, _author
from tests.utils.local_git_repository import LocalGitRepositoryFactory

GIT_PROBE = Twin("git/git", "main", {"README": "git (lifecycle twin of the apm doctor probe)\n"})
TWINS_CONSUMER_B: Mapping[str, Twin] = {twin.slug: twin for twin in (GIT_PROBE,)}


def publish_consumer_b_twins(services, root: Path, env: Mapping[str, str]) -> dict[str, Path]:
    """Publish every consumer_b twin under *root* and register it with *services*."""
    factory = LocalGitRepositoryFactory(root / "repositories", env=env)
    published: dict[str, Path] = {}
    for slug, twin in TWINS_CONSUMER_B.items():
        tree = root / "trees" / slug.replace("/", "__")
        _author(twin, tree)
        repository = factory.create(slug.replace("/", "__"), source_tree=tree)
        commit = factory.commit(repository, message=f"{slug} {twin.ref}")
        if twin.ref != "main":
            factory.tag(repository, twin.ref, commit)
        published[slug] = repository.origin
    services._repositories.update(published)
    return published
