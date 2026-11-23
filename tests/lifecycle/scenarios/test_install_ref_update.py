"""Updating the ref of an object-form dependency from the command line (#5154).

PROMISE: P-INV-I5 -- ``apm install <pkg>#<ref>`` on a dependency apm.yml
declares as an object changes only that entry's ``ref``; its other fields
(skills, targets, alias) are the user's and survive (source:
install/package_resolution.py merge_structured_entry_into_current_deps).

A bare ``apm install <pkg>`` (no ref) on that entry is already in apm.yml
and leaves it unchanged; that contract is kept here as a control.
"""

from __future__ import annotations

import pytest
import yaml

from tests.lifecycle import driver

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise("P-INV-I5"),
]

_SLUG = "lifecycle/p-skill"
_OBJECT = {
    "git": _SLUG,
    "ref": "v1.0.0",
    "skills": ["p-skill"],
    "targets": ["copilot"],
    "alias": "kit",
}


def _declare_object_entry(world) -> None:
    manifest = world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    data.setdefault("dependencies", {})["apm"] = [dict(_OBJECT)]
    manifest.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _declared(world) -> list:
    data = yaml.safe_load((world.project / "apm.yml").read_text(encoding="utf-8"))
    return data["dependencies"]["apm"]


def _text(result) -> str:
    return " ".join((result.stdout + result.stderr).split())


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        pytest.param(f"{_SLUG}#v2.0.0", [{**_OBJECT, "ref": "v2.0.0"}], id="ref-update-keeps"),
        pytest.param(_SLUG, [_OBJECT], id="bare-positional-unchanged"),
    ],
)
def test_install_with_a_ref_changes_only_the_ref_of_an_object_entry(
    lifecycle_world, spec: str, expected: list
) -> None:
    """``#v2.0.0`` keeps skills/targets/alias; a bare positional leaves the entry as is."""
    world = lifecycle_world
    world.services.publish_revision(_SLUG, {"CHANGELOG.md": "v2\n"}, tag="v2.0.0")
    _declare_object_entry(world)
    assert driver.run(world, ["install"]).returncode == 0

    result = driver.run(world, ["install", spec])

    assert result.returncode == 0, _text(result)
    assert _declared(world) == expected, _text(result)
