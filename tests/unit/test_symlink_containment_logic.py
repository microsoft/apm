"""Tests for symlink containment enforcement across APM subsystems (pure logic cases)."""

import typing
import unittest

import pytest

pytestmark = pytest.mark.unit


class TestIgnoreNonContentSourceGuard(unittest.TestCase):
    """Source-level guard: every copytree site that fans content out of
    apm_modules/ to deploy targets must use ``ignore_non_content`` so
    ``.apm-pin`` and symlinks cannot leak into the deploy tree.

    Companion to ``test_skill_integrator_native_skill_copytree_uses_ignore_non_content``
    which covers ``skill_integrator.py``; this class covers the remaining
    three modules (plugin_parser, packer, unpacker).
    """

    _MODULES: typing.ClassVar[list[tuple[str, str]]] = [
        ("apm_cli.deps", "plugin_parser"),
        ("apm_cli.bundle", "packer"),
        ("apm_cli.bundle", "unpacker"),
    ]

    def test_copytree_sites_use_ignore_non_content(self):
        """Every copytree call that copies into a deploy target must pass
        ``ignore_non_content`` (or a composing helper that wraps it).

        Source-level guard: if a future refactor drops the callback at any
        site, this test fails before a stale or malicious ``.apm-pin`` can
        leak into the deploy target.

        Asserted per call site via AST rather than by counting substrings: a
        hand-maintained occurrence floor silently loosens whenever call sites
        are merged or split, and it cannot tell a real argument from the same
        identifier mentioned in a docstring.
        """
        import ast
        import importlib
        import inspect

        def _is_shutil_copytree(node: ast.AST) -> bool:
            """Match ``shutil.copytree(...)`` and a bare imported ``copytree``.

            Deliberately not "any attribute named copytree": an unrelated
            helper exposing that name is not what this guard is about, and
            demanding ``ignore=`` from it would be a false failure.
            """
            if not isinstance(node, ast.Call):
                return False
            func = node.func
            if isinstance(func, ast.Attribute):
                return func.attr == "copytree" and (
                    isinstance(func.value, ast.Name) and func.value.id == "shutil"
                )
            return isinstance(func, ast.Name) and func.id == "copytree"

        def _guarded(node: ast.Call) -> bool:
            """True when the call's ``ignore=`` names the content filter.

            The argument's whole subtree is searched, so a composing helper
            (``ignore=_wrap(ignore_non_content)``) still counts while
            ``ignore=None`` or an unrelated callback does not. Presence of
            the keyword alone is not enough -- that was the hole the earlier
            occurrence count happened to cover.
            """
            for kw in node.keywords:
                if kw.arg != "ignore":
                    continue
                return any(
                    isinstance(sub, ast.Name) and sub.id == "ignore_non_content"
                    for sub in ast.walk(kw.value)
                )
            return False

        for pkg, mod_name in self._MODULES:
            with self.subTest(module=f"{pkg}.{mod_name}"):
                mod = importlib.import_module(f"{pkg}.{mod_name}")
                tree = ast.parse(inspect.getsource(mod))

                sites = [node for node in ast.walk(tree) if _is_shutil_copytree(node)]
                self.assertTrue(
                    sites,
                    f"{pkg}.{mod_name}: no copytree call sites found -- the guard "
                    "is no longer watching anything; retarget or remove it",
                )

                unguarded = [node.lineno for node in sites if not _guarded(node)]
                self.assertEqual(
                    unguarded,
                    [],
                    f"{pkg}.{mod_name}: copytree at line(s) {unguarded} do not pass "
                    "ignore=ignore_non_content -- .apm-pin and symlinks can leak "
                    "into the deploy tree",
                )


if __name__ == "__main__":
    unittest.main()
