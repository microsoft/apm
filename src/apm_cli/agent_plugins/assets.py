"""Bounded regular-file inventory for canonical Agent Plugin components."""

from __future__ import annotations

import hashlib
import os
import stat
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from ..utils.path_security import (
    PathTraversalError,
    ensure_path_within_resolved,
)
from .constants import MAX_COMPONENT_ASSET_BYTES, MAX_COMPONENT_ASSET_ENTRIES
from .ir import AgentPluginAsset, SourceProvenance

_READ_CHUNK_BYTES = 1024 * 1024


class AssetInventoryError(ValueError):
    """Raised when a component cannot produce a safe bounded inventory."""


class AssetInventory:
    """Collect immutable asset facts under one irreversible work budget."""

    def __init__(self, plugin_root: Path) -> None:
        self._root = plugin_root.resolve()
        self._entry_count = 0
        self._byte_count = 0
        self._assets: dict[str, AgentPluginAsset] = {}
        self._payloads: dict[str, bytes] = {}
        self._normalized_paths: dict[str, str] = {}

    def collect_component(self, component_root: Path) -> tuple[AgentPluginAsset, ...]:
        """Inventory one component without refunding attempted package work."""
        return self._collect(component_root)

    def list_component_candidates(self, directory: Path) -> tuple[Path, ...]:
        """Return deterministic direct children charged to the package work budget."""
        self._ensure_contained(directory)
        return self._bounded_directory_entries(directory)

    def collect_file(self, path: Path) -> AgentPluginAsset:
        """Inventory one declaration-referenced regular file."""
        self._ensure_contained(path)
        relative = self._relative_path(path)
        cached = self._assets.get(relative)
        if cached is not None:
            return cached
        self._assert_case_unambiguous(path)
        self._reserve_entry()
        try:
            initial = path.lstat()
        except OSError as exc:
            raise AssetInventoryError(f"asset metadata is unreadable: {exc}") from exc
        if stat.S_ISLNK(initial.st_mode):
            raise AssetInventoryError("symlinks are not accepted")
        if not stat.S_ISREG(initial.st_mode):
            raise AssetInventoryError("asset must be a regular file")
        self._record_normalized_path(relative)
        asset, _ = self._inventory_regular_file(path, initial)
        return asset

    def read_file(
        self,
        path: Path,
        *,
        max_bytes: int,
    ) -> tuple[AgentPluginAsset, bytes]:
        """Read and inventory one bounded regular file from the same descriptor."""
        self._ensure_contained(path)
        relative = self._relative_path(path)
        cached = self._assets.get(relative)
        cached_payload = self._payloads.get(relative)
        if cached is not None and cached_payload is not None:
            if len(cached_payload) > max_bytes:
                raise AssetInventoryError(f"asset exceeds the {max_bytes}-byte read limit")
            return cached, cached_payload
        if cached is None:
            self._reserve_entry()
            self._record_normalized_path(relative)
            self._assert_case_unambiguous(path)
        try:
            initial = path.lstat()
        except OSError as exc:
            raise AssetInventoryError(f"asset metadata is unreadable: {exc}") from exc
        if stat.S_ISLNK(initial.st_mode):
            raise AssetInventoryError("symlinks are not accepted")
        if not stat.S_ISREG(initial.st_mode):
            raise AssetInventoryError("asset must be a regular file")
        if initial.st_size > max_bytes:
            raise AssetInventoryError(f"asset exceeds the {max_bytes}-byte read limit")
        asset, payload = self._inventory_regular_file(path, initial, capture=True)
        self._payloads[relative] = payload
        return asset, payload

    def _collect(self, component_root: Path) -> tuple[AgentPluginAsset, ...]:
        self._reserve_entry()
        self._ensure_contained(component_root)
        self._record_normalized_path(self._relative_path(component_root))
        try:
            root_stat = component_root.lstat()
        except OSError as exc:
            raise AssetInventoryError(f"asset metadata is unreadable: {exc}") from exc
        if stat.S_ISLNK(root_stat.st_mode):
            raise AssetInventoryError("symlinks are not accepted")
        if stat.S_ISREG(root_stat.st_mode):
            asset, _ = self._inventory_regular_file(component_root, root_stat)
            return (asset,)
        if not stat.S_ISDIR(root_stat.st_mode):
            raise AssetInventoryError("component must be a regular file or directory")

        files: list[tuple[Path, os.stat_result]] = []
        pending = [component_root]
        while pending:
            directory = pending.pop()
            for entry in self._bounded_directory_entries(directory):
                self._ensure_contained(entry)
                try:
                    entry_stat = entry.lstat()
                except OSError as exc:
                    raise AssetInventoryError(f"asset metadata is unreadable: {exc}") from exc
                relative = self._relative_path(entry)
                self._record_normalized_path(relative)
                if stat.S_ISLNK(entry_stat.st_mode):
                    raise AssetInventoryError(f"asset {relative} is a symlink")
                if stat.S_ISDIR(entry_stat.st_mode):
                    pending.append(entry)
                    continue
                if not stat.S_ISREG(entry_stat.st_mode):
                    raise AssetInventoryError(f"asset {relative} is not a regular file")
                files.append((entry, entry_stat))

        assets: list[AgentPluginAsset] = []
        for path, initial in sorted(files, key=lambda item: self._relative_path(item[0])):
            asset, _ = self._inventory_regular_file(path, initial)
            assets.append(asset)
        return tuple(assets)

    def _bounded_directory_entries(self, directory: Path) -> tuple[Path, ...]:
        entries: list[Path] = []
        try:
            for entry in directory.iterdir():
                self._reserve_entry()
                entries.append(entry)
        except OSError as exc:
            raise AssetInventoryError(f"component directory is unreadable: {exc}") from exc
        return tuple(sorted(entries, key=lambda entry: entry.name))

    def _inventory_regular_file(
        self,
        path: Path,
        initial: os.stat_result,
        *,
        capture: bool = False,
    ) -> tuple[AgentPluginAsset, bytes]:
        relative = self._relative_path(path)
        cached = self._assets.get(relative)
        if cached is not None and not capture:
            return cached, b""
        if initial.st_size > MAX_COMPONENT_ASSET_BYTES:
            raise AssetInventoryError(
                f"asset {relative} exceeds the {MAX_COMPONENT_ASSET_BYTES}-byte package budget"
            )
        already_counted = cached.size if cached is not None else 0
        if self._byte_count - already_counted + initial.st_size > MAX_COMPONENT_ASSET_BYTES:
            raise AssetInventoryError(
                f"component assets exceed the {MAX_COMPONENT_ASSET_BYTES}-byte package budget"
            )

        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise AssetInventoryError(
                f"asset {relative} could not be opened safely: {exc}"
            ) from exc
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise AssetInventoryError(f"asset {relative} is not a regular file")
            if (initial.st_dev, initial.st_ino, initial.st_size) != (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
            ):
                raise AssetInventoryError(f"asset {relative} changed during inventory")
            digest = hashlib.sha256()
            bytes_read = 0
            payload = bytearray()
            while True:
                chunk = os.read(descriptor, _READ_CHUNK_BYTES)
                if not chunk:
                    break
                bytes_read += len(chunk)
                if bytes_read > initial.st_size:
                    raise AssetInventoryError(f"asset {relative} changed during inventory")
                if cached is None:
                    self._reserve_bytes(len(chunk))
                digest.update(chunk)
                if capture:
                    payload.extend(chunk)
        finally:
            os.close(descriptor)
        try:
            current = path.lstat()
        except OSError as exc:
            raise AssetInventoryError(f"asset {relative} changed during inventory: {exc}") from exc
        if (
            not stat.S_ISREG(current.st_mode)
            or bytes_read != initial.st_size
            or (current.st_dev, current.st_ino, current.st_size)
            != (initial.st_dev, initial.st_ino, initial.st_size)
            or current.st_mode & 0o111 != initial.st_mode & 0o111
        ):
            raise AssetInventoryError(f"asset {relative} changed during inventory")

        asset = AgentPluginAsset(
            path=relative,
            source=SourceProvenance(path=path, json_pointer=""),
            sha256=digest.hexdigest(),
            size=bytes_read,
            executable_mode=current.st_mode & 0o111,
        )
        if cached is not None:
            if _asset_identity(asset) != _asset_identity(cached):
                raise AssetInventoryError(f"asset {relative} changed during inventory")
            return cached, bytes(payload)
        self._assets[relative] = asset
        return asset, bytes(payload)

    def _reserve_entry(self) -> None:
        self._entry_count += 1
        if self._entry_count > MAX_COMPONENT_ASSET_ENTRIES:
            raise AssetInventoryError(
                f"component assets exceed the {MAX_COMPONENT_ASSET_ENTRIES}-entry package budget"
            )

    def _reserve_bytes(self, size: int) -> None:
        self._byte_count += size
        if self._byte_count > MAX_COMPONENT_ASSET_BYTES:
            raise AssetInventoryError(
                f"component assets exceed the {MAX_COMPONENT_ASSET_BYTES}-byte package budget"
            )

    def _ensure_contained(self, path: Path) -> None:
        try:
            ensure_path_within_resolved(path, self._root)
        except PathTraversalError as exc:
            raise AssetInventoryError(f"asset path escapes the plugin root: {path}") from exc

    def _relative_path(self, path: Path) -> str:
        try:
            return path.relative_to(self._root).as_posix()
        except ValueError as exc:
            raise AssetInventoryError(f"asset path escapes the plugin root: {path}") from exc

    def _assert_case_unambiguous(self, path: Path) -> None:
        try:
            relative_parts = path.relative_to(self._root).parts
        except ValueError as exc:
            raise AssetInventoryError(f"asset path escapes the plugin root: {path}") from exc
        parent = self._root
        for part in relative_parts:
            normalized = normalized_path_key(part)
            matches: list[str] = []
            try:
                for index, sibling in enumerate(parent.iterdir(), start=1):
                    if index > MAX_COMPONENT_ASSET_ENTRIES:
                        raise AssetInventoryError("asset parent exceeds the package entry budget")
                    if normalized_path_key(sibling.name) == normalized:
                        matches.append(sibling.name)
            except OSError as exc:
                raise AssetInventoryError(f"asset parent is unreadable: {exc}") from exc
            if len(set(matches)) != 1:
                raise AssetInventoryError(f"asset path is case-ambiguous at {parent / part}")
            parent /= part

    def _record_normalized_path(self, relative: str) -> None:
        normalized = normalized_path_key(relative)
        previous = self._normalized_paths.get(normalized)
        if previous is not None and previous != relative:
            raise AssetInventoryError(
                f"case-ambiguous or duplicate normalized paths: {previous}, {relative}"
            )
        self._normalized_paths[normalized] = relative

    @contextmanager
    def open_verified_asset(self, expected: AgentPluginAsset) -> Iterator[BinaryIO]:
        """Yield a verified descriptor using the inventory's resolved root."""
        with _open_verified_asset(self._root, expected) as file_handle:
            yield file_handle


@contextmanager
def open_verified_asset(
    plugin_root: Path,
    expected: AgentPluginAsset,
) -> Iterator[BinaryIO]:
    """Yield one verified descriptor so consumers never copy from a reopened path."""
    root = plugin_root.resolve()
    with _open_verified_asset(root, expected) as file_handle:
        yield file_handle


@contextmanager
def _open_verified_asset(
    root: Path,
    expected: AgentPluginAsset,
) -> Iterator[BinaryIO]:
    """Yield one verified descriptor relative to an already-resolved root."""
    path = root / Path(*expected.path.split("/"))
    try:
        ensure_path_within_resolved(path, root)
        initial = path.lstat()
    except (OSError, PathTraversalError) as exc:
        raise AssetInventoryError(f"asset {expected.path} cannot be verified: {exc}") from exc
    if stat.S_ISLNK(initial.st_mode) or not stat.S_ISREG(initial.st_mode):
        raise AssetInventoryError(f"asset {expected.path} is not a regular file")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AssetInventoryError(
            f"asset {expected.path} could not be opened safely: {exc}"
        ) from exc
    file_handle = os.fdopen(descriptor, "rb")
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_size != expected.size
            or opened.st_mode & 0o111 != expected.executable_mode
        ):
            raise AssetInventoryError(f"asset {expected.path} no longer matches its inventory")
        digest = hashlib.sha256()
        while chunk := file_handle.read(_READ_CHUNK_BYTES):
            digest.update(chunk)
        if digest.hexdigest() != expected.sha256:
            raise AssetInventoryError(f"asset {expected.path} no longer matches its inventory")
        file_handle.seek(0)
        yield file_handle
    finally:
        file_handle.close()


def _asset_identity(asset: AgentPluginAsset) -> tuple[str, str, int, int]:
    return asset.path, asset.sha256, asset.size, asset.executable_mode


def normalized_path_key(value: str) -> str:
    """Return the canonical NFC and case-insensitive path identity."""
    return unicodedata.normalize("NFC", value).casefold()
