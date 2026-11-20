"""Tests for apm_cli.policy.parser (load_policy, validate_policy) (filesystem, process and CLI boundary cases)."""

import os
import tempfile
import textwrap
import unittest

import pytest

from apm_cli.policy.parser import (
    load_policy,
)

pytestmark = pytest.mark.component


class TestLoadPolicyFromFile(unittest.TestCase):
    """Test load_policy from a file path."""

    def test_load_from_file(self):
        yaml_content = textwrap.dedent("""\
            name: file-policy
            version: "1.0"
            enforcement: off
        """)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            path = f.name

        try:
            policy, warnings = load_policy(path)  # noqa: RUF059
            self.assertEqual(policy.name, "file-policy")
            self.assertEqual(policy.enforcement, "off")
        finally:
            os.unlink(path)

    def test_load_from_pathlib_path(self):
        from pathlib import Path

        yaml_content = "name: pathlib-test\nversion: '0.1'"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            path = Path(f.name)

        try:
            policy, warnings = load_policy(path)  # noqa: RUF059
            self.assertEqual(policy.name, "pathlib-test")
        finally:
            os.unlink(str(path))


if __name__ == "__main__":
    unittest.main()
