"""RESEARCH ONLY (Lane N oracle O-resolve): every Path.resolve / os.path.realpath is lexical."""

from __future__ import annotations

import os
import pathlib


def install() -> None:
    def resolve(self, strict=False):
        return type(self)(os.path.abspath(self))

    pathlib.Path.resolve = resolve
    os.path.realpath = lambda path, *a, **k: os.path.abspath(path)
