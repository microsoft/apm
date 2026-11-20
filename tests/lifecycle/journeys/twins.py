"""Hermetic content twins of the upstream repositories the journeys install (#4110).

A twin has the same file layout and frontmatter keys as the upstream at the
documented ref, with short original bodies. ``publish_twins`` commits each
twin to a bare repository and registers it with the world's local GitHub
server, so ``apm install microsoft/apm-sample-package#v1.0.0`` resolves
inside the hermetic world.

Byte-shape fidelity against the real upstream (O10) is checked only by the
``live`` node ``test_twin_fidelity_o10`` in ``test_journeys.py``.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from tests.utils.local_git_repository import LocalGitRepositoryFactory

_REPO_ROOT = Path(__file__).parents[3]
RELEASE_TAG = "v" + next(
    line.split('"')[1]
    for line in (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines()
    if line.startswith("version = ")
)


@dataclass(frozen=True)
class Twin:
    slug: str  # owner/repo on github.com
    ref: str  # the tag the docs pin
    files: Mapping[str, str]  # relative path -> text
    copy_from: str = ""  # repo-relative directory copied under ``copy_to``
    copy_to: str = ""


def _md(front: str, body: str) -> str:
    return f"---\n{front}---\n\n{body}\n"


SAMPLE_PACKAGE = Twin(
    "microsoft/apm-sample-package",
    "v1.0.0",
    {
        "apm.yml": (
            "name: apm-sample-package\n"
            "version: 1.0.0\n"
            "description: Sample APM package with instructions, prompts, skills and agents.\n"
            "\n"
            "dependencies:\n"
            "  apm:\n"
            "    - github/awesome-copilot/skills/review-and-refactor\n"
        ),
        ".gitignore": "\n# APM dependencies\napm_modules/\n",
        "README.md": "# apm-sample-package (lifecycle twin)\n",
        ".apm/agents/design-reviewer.agent.md": _md(
            'description: "A design review specialist"\n', "# Design Reviewer\n\nReview designs."
        ),
        ".apm/instructions/design-standards.instructions.md": _md(
            'applyTo: "**"\n', "# Design Standards\n\nUse semantic HTML."
        ),
        ".apm/prompts/accessibility-audit.prompt.md": _md(
            'mode: agent\ndescription: "Audit code for accessibility"\n',
            "# Accessibility Audit\n\nAudit the code.",
        ),
        ".apm/prompts/design-review.prompt.md": _md(
            'mode: agent\ndescription: "Review code for design compliance"\n',
            "# Design Review\n\nReview the code.",
        ),
        ".apm/skills/style-checker/SKILL.md": "# Style Checker\n\nCheck code style.\n",
    },
)
AWESOME_COPILOT = Twin(
    "github/awesome-copilot",
    "main",
    {
        "README.md": "# awesome-copilot (lifecycle twin)\n",
        "skills/review-and-refactor/SKILL.md": _md(
            "name: review-and-refactor\ndescription: Review and refactor code.\n",
            "# Review and refactor\n\nReview, then refactor.",
        ),
    },
)
# The README pins the guide to the current release; the twin serves the real package.
APM_GUIDE = Twin(
    "microsoft/apm",
    RELEASE_TAG,
    {"README.md": "# apm (lifecycle twin)\n"},
    copy_from="packages/apm-guide",
    copy_to="packages/apm-guide",
)
TWINS: Mapping[str, Twin] = {
    twin.slug: twin for twin in (SAMPLE_PACKAGE, AWESOME_COPILOT, APM_GUIDE)
}


def _author(twin: Twin, tree: Path) -> None:
    for relative, text in twin.files.items():
        path = tree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="ascii")
    if twin.copy_from:
        shutil.copytree(_REPO_ROOT / twin.copy_from, tree / twin.copy_to)


def publish_twins(services, root: Path, env: Mapping[str, str]) -> dict[str, Path]:
    """Publish every twin under *root* and register it with *services* (once per session)."""
    factory = LocalGitRepositoryFactory(root / "repositories", env=env)
    published: dict[str, Path] = {}
    for slug, twin in TWINS.items():
        tree = root / "trees" / slug.replace("/", "__")
        _author(twin, tree)
        repository = factory.create(slug.replace("/", "__"), source_tree=tree)
        commit = factory.commit(repository, message=f"{slug} {twin.ref}")
        if twin.ref != "main":
            factory.tag(repository, twin.ref, commit)
        published[slug] = repository.origin
    # Core gap: WorldServices has no public publish(); see the T-8a report.
    services._repositories.update(published)
    return published
