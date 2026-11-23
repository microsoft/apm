"""A destination that leaves its root through a symlink is refused plainly (#5156).

PROMISE: P-INV-I7 -- a deploy destination reached through a symlinked path
component is refused, as before, with the standard message naming the link and
the next step instead of an internal-error text; a path outside the root keeps
its existing refusal (source: #5156 scope record).
owner: install/deployed_paths.py deployed_path_entry, through
utils/path_security.py first_symlink_component (has_symlink_component's walk)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.install.deployed_paths import deployed_path_entry

pytestmark = [pytest.mark.component, pytest.mark.promise("P-INV-I7")]


def test_a_symlinked_component_is_refused_with_the_standard_message(tmp_path: Path) -> None:
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    (outside / "demo").mkdir(parents=True)
    try:
        (root / "skills").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(RuntimeError) as refused:
        deployed_path_entry(root / "skills" / "demo" / "SKILL.md", root, [])

    message = str(refused.value)
    assert message == (
        "Refusing deployment through a symlinked path component: skills. "
        "Replace the symlink with a regular directory or remove it, then retry."
    )


def test_a_path_outside_the_root_keeps_its_refusal(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    with pytest.raises(RuntimeError, match="Cannot translate"):
        deployed_path_entry(tmp_path / "elsewhere" / "SKILL.md", root, [])


def test_the_message_names_the_first_linked_component_at_any_depth(tmp_path: Path) -> None:
    # The owner's walk (first_symlink_component) picks the component; a deeper
    # link below it is never the one reported.
    root, outside = tmp_path / "root", tmp_path / "outside"
    (root / "plain").mkdir(parents=True)
    (outside / "inner").mkdir(parents=True)
    try:
        (root / "plain" / "linked").symlink_to(outside, target_is_directory=True)
        (outside / "deeper").symlink_to(outside / "inner", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(RuntimeError) as refused:
        deployed_path_entry(root / "plain" / "linked" / "deeper" / "SKILL.md", root, [])

    assert str(refused.value).startswith(
        "Refusing deployment through a symlinked path component: plain/linked. "
    )
