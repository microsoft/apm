"""Reading bundle Markdown frontmatter for ``hooks`` (#5248)."""

from __future__ import annotations

from pathlib import Path

import pytest

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
        # Undecodable frontmatter fails closed even where a converter would judge YAML.
        pytest.param("a.md", b"---\nname: \xff\n---\nbody\n", True, True, id="bad-fm-bytes"),
        pytest.param("a.md", b"---\nname: x\n---\n\xff\xfe\n", False, False, id="bad-body-bytes"),
        pytest.param("a.md", b"\xef\xbb\xbf---\nhooks: {}\n---\n", True, True, id="bom"),
        pytest.param(
            "a.md",
            b"---\npad: " + b"a" * _LONG + b"\nhooks: {}\n---\nbody\n",
            True,
            True,
            id="fence-closes-past-prefix",
        ),
        # Any packed file is checked: a .txt command still deploys as Markdown.
        pytest.param("a.txt", b"---\nhooks: {}\n---\n", True, True, id="txt-with-hooks"),
        pytest.param("a.json", b'{"hooks": {}}\n', False, False, id="json-no-fence"),
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
    assert reads == [4096] * 2


def _assert_skipped_as_hooks(rel: str, source: Path) -> None:
    from apm_cli.install.local_bundle_paths import (
        bundle_deploy_relative_path,
        bundle_deploy_skip_warning,
    )
    from apm_cli.integration.targets import KNOWN_TARGETS

    route = {
        "allowed_prefixes": frozenset(),
        "known_deploy_prefixes": frozenset(),
        "target": KNOWN_TARGETS["claude"],
        "hooks_approved": False,
        "canvas_approved": False,
        "source": source,
    }
    assert bundle_deploy_relative_path(rel, **route) is None
    warning = bundle_deploy_skip_warning(rel, **route)
    assert warning is not None
    assert f"Skipped packed bundle hooks file {rel!r}" in warning


def test_converted_instruction_with_undecodable_frontmatter_is_skipped_as_hooks(
    tmp_path: Path,
) -> None:
    rel = "instructions/bad.instructions.md"
    source = tmp_path / rel
    source.parent.mkdir(parents=True)
    source.write_bytes(b"---\napplyTo: '**'\nnote: \xff\n---\nbody\n")

    _assert_skipped_as_hooks(rel, source)


def test_txt_command_with_frontmatter_hooks_is_skipped_as_hooks(tmp_path: Path) -> None:
    rel = "commands/run.txt"
    source = tmp_path / rel
    source.parent.mkdir(parents=True)
    source.write_bytes(b"---\ndescription: Run\nhooks: {}\n---\nRun.\n")

    _assert_skipped_as_hooks(rel, source)


@pytest.mark.parametrize(
    ("content", "block"),
    [
        pytest.param(b"---\nname: x\n---\nbody\n", "---\nname: x\n---", id="closed"),
        pytest.param(b"\xef\xbb\xbf---\nname: x\n---\n", "---\nname: x\n---\n", id="bom"),
        pytest.param(b"# body\n", "", id="no-fence"),
    ],
)
def test_frontmatter_block_is_read_by_the_yaml_owner(
    tmp_path: Path, content: bytes, block: str
) -> None:
    from apm_cli.utils.yaml_io import read_frontmatter_block

    path = tmp_path / "a.md"
    path.write_bytes(content)

    _assert_block(read_frontmatter_block(path), block)


def _assert_block(actual: str, expected: str) -> None:
    assert actual == expected
