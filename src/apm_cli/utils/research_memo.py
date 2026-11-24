"""RESEARCH ONLY (perf-research/arch-single-pass): invocation-scoped read/hash memo.

Upper bound for a single-pass integrate: each package file under apm_modules or the
checkout cache is read once and hashed once per invocation. Not faithful: no
TOCTOU proof, no no-follow reads. Never land.
"""

from __future__ import annotations

import os
import pathlib

_SEP = os.sep
_BYTES: dict[str, bytes] = {}
_HASH: dict[tuple, object] = {}
STATS = {"bytes_hit": 0, "bytes_miss": 0, "hash_hit": 0, "hash_miss": 0}


def eligible(path: object) -> bool:
    text = os.fspath(path)
    if f"{_SEP}apm_modules{_SEP}" not in text and "checkouts_v1" not in text:
        return False
    name = os.path.basename(text)
    if name.startswith(".") or f"{_SEP}.apm" in text.split("apm_modules", 1)[-1]:
        return False
    return not name.endswith((".lock", ".lock.yaml", ".marker"))


def cached_bytes(path: object, read) -> bytes:
    key = os.fspath(path)
    data = _BYTES.get(key)
    if data is None:
        STATS["bytes_miss"] += 1
        data = read()
        _BYTES[key] = data
    else:
        STATS["bytes_hit"] += 1
    return data


def memo_hash(key: tuple, compute):
    value = _HASH.get(key)
    if value is None:
        STATS["hash_miss"] += 1
        value = compute()
        _HASH[key] = value
    else:
        STATS["hash_hit"] += 1
    return value


def install() -> None:
    original_read_bytes = pathlib.Path.read_bytes
    original_read_text = pathlib.Path.read_text

    def read_bytes(self):
        if eligible(self):
            return cached_bytes(self, lambda: original_read_bytes(self))
        return original_read_bytes(self)

    def read_text(self, encoding=None, errors=None, newline=None):
        if encoding is None or newline is not None or not eligible(self):
            return original_read_text(self, encoding=encoding, errors=errors)
        data = cached_bytes(self, lambda: original_read_bytes(self))
        text = data.decode(encoding, errors or "strict")
        return text.replace("\r\n", "\n").replace("\r", "\n")

    pathlib.Path.read_bytes = read_bytes
    pathlib.Path.read_text = read_text
