"""Compatibility shim: in-place restore lives in ``tests.lifecycle.snapshot`` (#4093)."""

from tests.lifecycle.snapshot import restore_tree

__all__ = ["restore_tree"]
