"""Reading bundle Markdown frontmatter for ``hooks`` (#5248)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.install import local_bundle_paths
from apm_cli.install.local_bundle_paths import markdown_declares_hooks

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-local-bundle-hook-trust")]

_LONG = 10_000


def _assert_declares(path: Path, expected: bool, expected_when_converted: bool) -> None:
    assert markdown_declares_hooks(path) is expected
    assert markdown_declares_hooks(path, unreadable=False) is expected_when_converted


@pytest.mark.parametrize(
    ("name", "content", "expected", "expected_when_converted"),
    [
        pytest.param("a.md", b"---\nhooks: {}\n---\nbody\n", True, True, id="hooks"),
        pytest.param("a.md", b"---\nname: x\n---\nbody\n", False, False, id="no-hooks"),
        pytest.param("a.md", b"# body\n", False, False, id="no-frontmatter"),
        pytest.param("a.md", b"---\nname: [x\n---\nbody\n", True, False, id="bad-yaml"),
        pytest.param("a.md", b"---\nname: x\nbody\n", True, False, id="unclosed"),
        pytest.param("a.md", b"---\nname: \xff\n---\nbody\n", True, False, id="bad-fm-bytes"),
        pytest.param("a.md", b"---\nname: x\n---\n\xff\xfe\n", False, False, id="bad-body-bytes"),
        pytest.param("a.md", b"\xef\xbb\xbf---\nhooks: {}\n---\n", True, True, id="bom"),
        pytest.param(
            "a.md",
            b"---\npad: " + b"a" * _LONG + b"\nhooks: {}\n---\nbody\n",
            True,
            True,
            id="fence-closes-past-prefix",
        ),
        pytest.param("a.txt", b"---\nhooks: {}\n---\n", False, False, id="not-markdown"),
    ],
)
def test_frontmatter_hooks_are_read_from_the_fenced_block(
    tmp_path: Path, name: str, content: bytes, expected: bool, expected_when_converted: bool
) -> None:
    path = tmp_path / name
    path.write_bytes(content)

    _assert_declares(path, expected, expected_when_converted)


def test_only_a_prefix_is_read_when_the_fence_closes_inside_it(tmp_path: Path, monkeypatch):
    path = tmp_path / "a.md"
    path.write_bytes(b"---\nhooks: {}\n---\n" + b"b" * _LONG + b"\xff\n")
    reads: list[int] = []
    real_open = Path.open

    class CountingReader:
        def __init__(self, handle):
            self._handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._handle.close()

        def read(self, size=-1):
            data = self._handle.read(size)
            reads.append(len(data))
            return data

    def counting_open(self, *args, **kwargs):
        return CountingReader(real_open(self, *args, **kwargs))

    monkeypatch.setattr(Path, "open", counting_open)

    _assert_declares(path, True, True)
    assert reads == [local_bundle_paths._FRONTMATTER_PREFIX_BYTES] * 2
