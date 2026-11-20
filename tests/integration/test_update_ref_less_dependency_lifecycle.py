"""APMLifecycle: `apm update` converges on a dependency declared without a ref (#4166).

PROMISE: outcome.status_matches_state + idempotency.byte_stable -- an update that changes nothing reports
nothing updated and leaves every byte in place (source: issue #4166).
CELL: OP=update SRC=git(ref absent -> remote default branch) PRIM=skill
SCOPE={project,user} LOCK=present SHAPE={direct, transitive virtual sub-path under a tag}
owner: dependency-identity-materialization (install/plan.py build_update_plan)
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.integration.test_required_lifecycle_state_machine import (
    _OWNER,
    _new_scenario,
    _publish,
    _publish_revision,
    _skill,
)
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I2", "P-LAW-outcome.status_matches_state"),
]


def _ref_less_world(scenario, shape: str):
    """Return (published child, manifest dependency, env) for *shape*."""
    published = _publish(scenario, "kit", skill="review")
    if shape == "direct":
        # No ref: the dependency follows the remote default branch.
        dependency = {key: value for key, value in published.dependency.items() if key != "ref"}
        return published, dependency, published.environment
    # A parent at a tag declares a ref-less virtual sub-path of the child.
    parent = scenario.sources.create("parent")
    scenario.sources.add_skill(parent, "parent-skill", _skill("parent-skill"))
    manifest = yaml.safe_load(parent.manifest_path.read_text(encoding="utf-8"))
    manifest.setdefault("dependencies", {})["apm"] = [f"{_OWNER}/kit/skills/review"]
    parent.manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    repository = scenario.repositories.create("parent", source_tree=parent.root)
    commit = scenario.repositories.commit(repository, message="seed parent")
    scenario.repositories.tag(repository, "v1.0.0", commit, annotated=True)
    parent_url = f"https://github.com/{_OWNER}/parent"
    env = scenario.repositories.url_rewrite_subprocess_env_many(
        ((published.repository, published.remote_url), (repository, parent_url))
    )
    return published, {"git": parent_url, "ref": "v1.0.0"}, env


@pytest.mark.parametrize("shape", ["direct", "transitive-subpath"])
@pytest.mark.parametrize("scope", ["project", "user"])
def test_update_of_a_ref_less_dependency_reports_nothing_and_changes_no_bytes(
    tmp_path: Path, apm_binary_path: Path, scope: str, shape: str
) -> None:
    scenario = _new_scenario(tmp_path / "world", apm_binary_path)
    published, dependency, child_env = _ref_less_world(scenario, shape)
    env = dict(child_env, COLUMNS="400")
    if scope == "project":
        consumer = scenario.consumers.create("consumer", targets=("copilot",))
        manifest, cwd, flags = consumer.manifest_path, consumer.root, ()
        roots = (consumer.root,)
    else:
        manifest = scenario.isolated.config_root / "apm.yml"
        manifest.write_text(
            yaml.safe_dump({"name": "user", "version": "0.0.0", "targets": ["copilot"]}),
            encoding="utf-8",
        )
        cwd, flags = scenario.isolated.work_root, ("-g",)
        roots = (scenario.isolated.home,)
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data.setdefault("dependencies", {})["apm"] = [dependency]
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")

    def apm(*argv: str):
        result = scenario.runner.run((*argv, *flags), cwd=cwd, env=env)
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert result.stdout.isascii() and result.stderr.isascii()
        return result

    apm("install")
    for attempt in (1, 2):
        before = [ArtifactSnapshot.capture(root) for root in roots]
        update = apm("update", "--yes")
        # I7 + I2: nothing moved upstream, so nothing is reported or rewritten.
        assert "1 updated" not in update.stdout, (attempt, update.stdout)
        assert "->" not in update.stdout, (attempt, update.stdout)
        assert "already at their latest matching refs" in update.stdout, (attempt, update.stdout)
        for root, snapshot in zip(roots, before, strict=True):
            assert_unchanged(snapshot, ArtifactSnapshot.capture(root))

    # The default branch moves: the same update now reports the new commit.
    moved = _publish_revision(scenario, published, "2")
    update = apm("update", "--yes")
    assert "1 updated" in update.stdout, update.stdout
    assert moved.sha[:7] in update.stdout, update.stdout
