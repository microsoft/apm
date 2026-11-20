"""Hermetic twins of the org policy journey E01 (#4110).

``contoso/.github`` serves the draft policy of enterprise/apm-policy.mdx
(``enforcement: warn``); E01 later commits ``enforcement: block`` to it with
the runner's ``serve`` edit. ``acme/evil-pkg`` is a package outside the
policy's allow list, served so a refusal is about policy, not a missing repo.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from tests.lifecycle.journeys.twins import Twin, _md
from tests.utils.local_git_repository import LocalGitRepositoryFactory

POLICY_YAML = """name: contoso-agent-policy
version: "1.0.0"
enforcement: warn
dependencies:
  allow:
    - "github.com/contoso/**"
    - "github.com/microsoft/**"
  deny:
    - "github.com/untrusted-org/**"
"""

TWINS_POLICY: Mapping[str, Twin] = {
    twin.slug: twin
    for twin in (
        Twin("contoso/.github", "main", {"apm-policy.yml": POLICY_YAML}),
        Twin(
            "acme/evil-pkg",
            "v1.0.0",
            {
                "apm.yml": "name: evil-pkg\nversion: 1.0.0\ndescription: Outside the allow list.\n",
                "skills/evil-pkg/SKILL.md": _md(
                    "name: evil-pkg\ndescription: A skill outside the allow list.\n", "Use it."
                ),
            },
        ),
    )
}


def publish_policy_twins(services, root: Path, env: Mapping[str, str]) -> dict[str, Path]:
    """Publish the E01 twins under *root* and register them with *services*."""
    factory = LocalGitRepositoryFactory(root / "repositories", env=env)
    published: dict[str, Path] = {}
    for slug, twin in TWINS_POLICY.items():
        tree = root / "trees" / slug.replace("/", "__")
        for relative, text in twin.files.items():
            path = tree / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="ascii")
        repository = factory.create(slug.replace("/", "__"), source_tree=tree)
        commit = factory.commit(repository, message=f"{slug} {twin.ref}")
        if twin.ref != "main":
            factory.tag(repository, twin.ref, commit)
        published[slug] = repository.origin
    services._repositories.update(published)
    return published
