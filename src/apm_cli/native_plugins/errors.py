"""Errors raised by the harness-neutral native plugin owner."""

from __future__ import annotations


class NativePluginError(RuntimeError):
    """Native activation cannot safely honor the requested plugins.

    Harness owners subclass it (``ClaudeNativePluginError``) so one install
    passthrough surfaces every native diagnostic verbatim.
    """
