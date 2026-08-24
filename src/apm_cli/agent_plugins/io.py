"""Bounded JSON loading helpers for Agent Plugins documents."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

MAX_JSON_BYTES = 5 * 1024 * 1024


def read_json_document(path: Path, *, reject_duplicate_schema: bool = False) -> Any:
    """Read and decode a JSON file with a fixed size cap."""
    try:
        initial = path.lstat()
    except OSError as exc:
        raise ValueError(f"JSON file {path} metadata is unreadable: {exc}") from exc
    if stat.S_ISLNK(initial.st_mode):
        raise ValueError(f"JSON file {path} must not be a symlink")
    if not stat.S_ISREG(initial.st_mode):
        raise ValueError(f"JSON file {path} must be a regular file")
    if initial.st_size > MAX_JSON_BYTES:
        raise ValueError(
            f"JSON file {path} exceeds {MAX_JSON_BYTES}-byte cap ({initial.st_size} bytes)"
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"JSON file {path} could not be opened safely: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError(f"JSON file {path} must be a regular file")
        if (initial.st_dev, initial.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError(f"JSON file {path} changed during validation")
        try:
            current = path.lstat()
        except OSError as exc:
            raise ValueError(f"JSON file {path} changed during validation: {exc}") from exc
        if not stat.S_ISREG(current.st_mode) or (
            current.st_dev,
            current.st_ino,
        ) != (opened.st_dev, opened.st_ino):
            raise ValueError(f"JSON file {path} changed during validation")
        payload = _read_bounded(descriptor)
    finally:
        os.close(descriptor)
    if len(payload) > MAX_JSON_BYTES:
        raise ValueError(f"JSON file {path} exceeds {MAX_JSON_BYTES}-byte cap")
    return decode_json_document(
        payload,
        path=path,
        reject_duplicate_schema=reject_duplicate_schema,
    )


def decode_json_document(
    payload: bytes,
    *,
    path: Path,
    reject_duplicate_schema: bool = False,
) -> Any:
    """Decode bounded JSON bytes already read from a trusted descriptor."""
    try:
        text = payload.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_schema if reject_duplicate_schema else None,
        )
    except (ValueError, RecursionError, MemoryError) as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc


def _read_bounded(descriptor: int) -> bytes:
    payload = bytearray()
    while len(payload) <= MAX_JSON_BYTES:
        chunk = os.read(descriptor, min(64 * 1024, MAX_JSON_BYTES + 1 - len(payload)))
        if not chunk:
            break
        payload.extend(chunk)
    return bytes(payload)


def _reject_duplicate_schema(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    schema_seen = False
    for key, value in pairs:
        if key == "$schema":
            if schema_seen:
                raise ValueError("duplicate $schema field")
            schema_seen = True
        document[key] = value
    return document
