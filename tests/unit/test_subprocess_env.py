"""Tests for :mod:`apm_cli.utils.subprocess_env`.

These tests lock in the contract documented in the module's docstring:
the helper must be a no-op outside a frozen build, and must restore
every PyInstaller-managed library-path variable from its ``_ORIG``
sibling (or drop it) inside a frozen build, without disturbing any
other inherited variable.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

from apm_cli.utils.subprocess_env import external_process_env


class TestExternalProcessEnvNotFrozen(unittest.TestCase):
    """Outside a frozen build the helper is a pure ``dict`` copy."""

    def test_returns_independent_copy_of_os_environ(self):
        with patch.object(sys, "frozen", False, create=True):
            env = external_process_env()
        self.assertIsNot(env, os.environ)
        self.assertEqual(env, dict(os.environ))

    def test_leaves_library_path_vars_alone_when_not_frozen(self):
        base = {
            "PATH": "/usr/bin",
            "LD_LIBRARY_PATH": "/bundle/_internal",
            "LD_LIBRARY_PATH_ORIG": "/usr/lib",
            "DYLD_LIBRARY_PATH": "/bundle/_internal",
        }
        with patch.object(sys, "frozen", False, create=True):
            env = external_process_env(base)
        self.assertEqual(env, base)
        self.assertIsNot(env, base)

    def test_frozen_attribute_absent_is_treated_as_not_frozen(self):
        base = {"LD_LIBRARY_PATH": "/bundle/_internal"}
        # Ensure sys.frozen is truly absent for this call.
        had_frozen = hasattr(sys, "frozen")
        prior = getattr(sys, "frozen", None)
        if had_frozen:
            del sys.frozen  # type: ignore[attr-defined]
        try:
            env = external_process_env(base)
        finally:
            if had_frozen:
                sys.frozen = prior  # type: ignore[attr-defined]
        self.assertEqual(env, base)


class TestExternalProcessEnvFrozen(unittest.TestCase):
    """Inside a frozen build the helper restores the library-path vars."""

    def test_restores_ld_library_path_from_orig(self):
        base = {
            "PATH": "/usr/bin",
            "LD_LIBRARY_PATH": "/usr/local/lib/apm/_internal",
            "LD_LIBRARY_PATH_ORIG": "/usr/lib/x86_64-linux-gnu",
        }
        with patch.object(sys, "frozen", True, create=True):
            env = external_process_env(base)
        self.assertEqual(env["LD_LIBRARY_PATH"], "/usr/lib/x86_64-linux-gnu")
        self.assertNotIn("LD_LIBRARY_PATH_ORIG", env)
        # PATH must not be touched.
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_drops_ld_library_path_when_no_orig(self):
        """No ``_ORIG`` sibling means the user had no pre-launch value.

        Scenario that triggered issue #894: PyInstaller set
        ``LD_LIBRARY_PATH`` but the user never exported one, so the only
        correct restoration is to remove the variable entirely.
        """
        base = {
            "PATH": "/usr/bin",
            "LD_LIBRARY_PATH": "/usr/local/lib/apm/_internal",
        }
        with patch.object(sys, "frozen", True, create=True):
            env = external_process_env(base)
        self.assertNotIn("LD_LIBRARY_PATH", env)
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_preserves_user_exported_empty_orig(self):
        """An empty ``_ORIG`` reflects the user having no export; honour it."""
        base = {
            "LD_LIBRARY_PATH": "/bundle",
            "LD_LIBRARY_PATH_ORIG": "",
        }
        with patch.object(sys, "frozen", True, create=True):
            env = external_process_env(base)
        # The restored value is the original (empty string), which is
        # semantically different from "unset" but matches what the user's
        # shell had at launch -- we honour it rather than second-guessing.
        self.assertEqual(env["LD_LIBRARY_PATH"], "")
        self.assertNotIn("LD_LIBRARY_PATH_ORIG", env)

    def test_handles_all_dyld_variants(self):
        base = {
            "DYLD_LIBRARY_PATH": "/bundle",
            "DYLD_LIBRARY_PATH_ORIG": "/usr/local/lib",
            "DYLD_FRAMEWORK_PATH": "/bundle",
            # No DYLD_FRAMEWORK_PATH_ORIG -> should be dropped.
        }
        with patch.object(sys, "frozen", True, create=True):
            env = external_process_env(base)
        self.assertEqual(env["DYLD_LIBRARY_PATH"], "/usr/local/lib")
        self.assertNotIn("DYLD_LIBRARY_PATH_ORIG", env)
        self.assertNotIn("DYLD_FRAMEWORK_PATH", env)

    def test_noop_when_no_library_path_vars_present(self):
        base = {"PATH": "/usr/bin", "HOME": "/home/user"}
        with patch.object(sys, "frozen", True, create=True):
            env = external_process_env(base)
        self.assertEqual(env, base)
        self.assertIsNot(env, base)

    def test_base_mapping_overrides_os_environ(self):
        """``base`` must take precedence over the live environment."""
        # Inject noise into os.environ that the helper must ignore when
        # ``base`` is supplied.
        with (
            patch.dict(
                os.environ,
                {"LD_LIBRARY_PATH": "/noise", "LD_LIBRARY_PATH_ORIG": "/noise_orig"},
                clear=False,
            ),
            patch.object(sys, "frozen", True, create=True),
        ):
            base = {
                "LD_LIBRARY_PATH": "/bundle",
                "LD_LIBRARY_PATH_ORIG": "/real_orig",
            }
            env = external_process_env(base)
        self.assertEqual(env["LD_LIBRARY_PATH"], "/real_orig")
        self.assertNotIn("LD_LIBRARY_PATH_ORIG", env)

    def test_does_not_mutate_input_mapping(self):
        base = {
            "LD_LIBRARY_PATH": "/bundle",
            "LD_LIBRARY_PATH_ORIG": "/usr/lib",
        }
        snapshot = dict(base)
        with patch.object(sys, "frozen", True, create=True):
            external_process_env(base)
        self.assertEqual(base, snapshot)

    def test_does_not_mutate_os_environ(self):
        with (
            patch.dict(
                os.environ,
                {
                    "LD_LIBRARY_PATH": "/bundle",
                    "LD_LIBRARY_PATH_ORIG": "/usr/lib",
                },
                clear=False,
            ),
            patch.object(sys, "frozen", True, create=True),
        ):
            snapshot = dict(os.environ)
            external_process_env()
            self.assertEqual(dict(os.environ), snapshot)


if __name__ == "__main__":
    unittest.main()
