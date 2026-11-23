"""Owner containment checks reach utils/paths' string route, never pathlib's walk.

With pathlib's own ``relative_to`` and ``is_relative_to`` made to fail, the
path_security checks, ``portable_relpath`` and the package content hash still
answer for resolved, same-class absolute paths on a real directory tree.
"""

from __future__ import annotations

import os
from pathlib import PurePath

import pytest

from apm_cli.utils import content_hash, path_security, paths

pytestmark = [pytest.mark.component, pytest.mark.promise("P-LAW-portability.byte_deterministic")]


@pytest.fixture
def no_pathlib_containment(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args, **_kwargs):
        raise AssertionError("pathlib containment was used for a common-shape input")

    monkeypatch.setattr(PurePath, "relative_to", refuse)
    monkeypatch.setattr(PurePath, "is_relative_to", refuse)


@pytest.mark.skipif(os.name == "nt", reason="the unit property test covers Windows")
def test_owner_containment_checks_take_the_string_route(
    tmp_path, no_pathlib_containment: None
) -> None:
    root = tmp_path.resolve()
    inner = root / "pkg" / "x.md"
    inner.parent.mkdir()
    inner.write_text("x\n", encoding="utf-8")
    assert path_security.resolved_relative_within(inner.resolve(), root) == "pkg/x.md"
    assert path_security.ensure_path_within_resolved(inner, root) == inner.resolve()
    assert path_security.has_symlink_component(root, inner) is False
    assert paths.portable_relpath(inner, root) == "pkg/x.md"
    assert content_hash.compute_package_hash(root / "pkg").startswith("sha256:")
    # The checks only read: the tree they inspected is left exactly as it was.
    assert [entry.name for entry in (root / "pkg").iterdir()] == ["x.md"]
    assert inner.read_text(encoding="utf-8") == "x\n"
