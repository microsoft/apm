"""Unit tests for compilation/build_id.py.

Tests the stabilize_build_id() helper that replaces the BUILD_ID_PLACEHOLDER
line with a deterministic 12-char SHA256 hash computed over the content with
the placeholder line removed (so the hash is not self-referential).
"""

import re

import pytest  # noqa: F401

from apm_cli.compilation.build_id import stabilize_build_id
from apm_cli.compilation.constants import BUILD_ID_PLACEHOLDER

_HASH_LINE_RE = re.compile(r"^<!-- Build ID: [a-f0-9]{12} -->$")


def test_replaces_placeholder_with_hash_line():
    content = f"# AGENTS.md\n{BUILD_ID_PLACEHOLDER}\n<!-- APM Version: 1.0.0 -->\n"

    result = stabilize_build_id(content)

    assert BUILD_ID_PLACEHOLDER not in result
    hash_line = result.splitlines()[1]
    assert _HASH_LINE_RE.match(hash_line), f"unexpected line: {hash_line!r}"


def test_returns_unchanged_when_no_placeholder():
    content = "# AGENTS.md\n<!-- APM Version: 1.0.0 -->\nbody\n"

    assert stabilize_build_id(content) == content


def test_idempotent_after_one_pass():
    content = f"# AGENTS.md\n{BUILD_ID_PLACEHOLDER}\nbody\n"

    once = stabilize_build_id(content)
    twice = stabilize_build_id(once)

    assert once == twice


def test_deterministic_for_same_input():
    content = f"# AGENTS.md\n{BUILD_ID_PLACEHOLDER}\nbody\n"

    assert stabilize_build_id(content) == stabilize_build_id(content)


def test_different_content_yields_different_hash():
    content_a = f"# A\n{BUILD_ID_PLACEHOLDER}\nbody-a\n"
    content_b = f"# B\n{BUILD_ID_PLACEHOLDER}\nbody-b\n"

    hash_a = stabilize_build_id(content_a).splitlines()[1]
    hash_b = stabilize_build_id(content_b).splitlines()[1]

    assert hash_a != hash_b


def test_hash_excludes_placeholder_line_itself():
    """Hash must be computed over content with the placeholder line removed,
    otherwise the hash is self-referential and cannot remain stable across
    formatter versions that change the placeholder string.

    Two contents that differ ONLY in the placeholder position must hash
    identically when the placeholder is excluded from the hash input.
    """
    content_a = f"# A\n{BUILD_ID_PLACEHOLDER}\nbody\n"
    content_b = f"{BUILD_ID_PLACEHOLDER}\n# A\nbody\n"

    hash_a = stabilize_build_id(content_a).splitlines()
    hash_b = stabilize_build_id(content_b).splitlines()

    hash_a_value = next(line for line in hash_a if line.startswith("<!-- Build ID:"))
    hash_b_value = next(line for line in hash_b if line.startswith("<!-- Build ID:"))

    assert hash_a_value == hash_b_value


def test_preserves_trailing_newline():
    with_nl = f"# A\n{BUILD_ID_PLACEHOLDER}\nbody\n"
    without_nl = f"# A\n{BUILD_ID_PLACEHOLDER}\nbody"

    assert stabilize_build_id(with_nl).endswith("\n")
    assert not stabilize_build_id(without_nl).endswith("\n")


def test_empty_content_is_safe():
    assert stabilize_build_id("") == ""


def test_only_placeholder_line():
    content = f"{BUILD_ID_PLACEHOLDER}\n"

    result = stabilize_build_id(content)

    assert BUILD_ID_PLACEHOLDER not in result
    assert _HASH_LINE_RE.match(result.splitlines()[0])
