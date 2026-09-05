"""Canonical owner for ``.apmignore`` package membership.

Every install-deploy copy, pack walk, and compile/discovery walk that
needs to know whether a path ships must call :class:`ApmIgnoreSpec`.
Do not parse ``.apmignore`` anywhere else.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pathspec import GitIgnoreSpec

from apm_cli.constants import (
    APM_IGNORE_FILENAME,
    APM_YML_FILENAME,
    DEFAULT_SKIP_DIRS,
    SKILL_MD_FILENAME,
)
from apm_cli.utils.paths import portable_relpath

_REQUIRED_ROOT_FILES = (SKILL_MD_FILENAME, APM_YML_FILENAME)
_LOAD_CACHE: dict[str, ApmIgnoreSpec] = {}

CopyIgnore = Callable[[str, list[str]], list[str]]


class ApmIgnoreError(ValueError):
    """Raised when ``.apmignore`` would drop a required package file."""


@dataclass(frozen=True)
class _IgnoreLayer:
    """One ``.apmignore`` file, scoped to the directory that contains it."""

    directory: Path
    rel_prefix: str
    spec: GitIgnoreSpec


class ApmIgnoreSpec:
    """Loaded ``.apmignore`` rules for one package root."""

    def __init__(self, package_root: Path, layers: tuple[_IgnoreLayer, ...]) -> None:
        self.package_root = package_root
        self._layers = layers

    @classmethod
    def load(cls, package_root: Path) -> ApmIgnoreSpec:
        """Load root and nested ``.apmignore`` files under *package_root*."""
        try:
            root = package_root.resolve()
        except (OSError, RuntimeError):
            root = package_root.absolute()
        cache_key = str(root)
        cached = _LOAD_CACHE.get(cache_key)
        if cached is not None:
            return cached
        layers: list[_IgnoreLayer] = []
        if root.is_dir():
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                current = Path(dirpath)
                dirnames[:] = sorted(
                    name
                    for name in dirnames
                    if name not in DEFAULT_SKIP_DIRS and not (current / name).is_symlink()
                )
                if APM_IGNORE_FILENAME not in filenames:
                    continue
                ignore_file = current / APM_IGNORE_FILENAME
                if ignore_file.is_symlink() or not ignore_file.is_file():
                    raise ApmIgnoreError(
                        f"Cannot load {APM_IGNORE_FILENAME} at {ignore_file}: "
                        "must be a regular file, not a symlink or directory"
                    )
                rel_prefix = portable_relpath(current, root)
                if rel_prefix in {".", ""} or rel_prefix == current.as_posix():
                    rel_prefix = ""
                try:
                    text = ignore_file.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as exc:
                    raise ApmIgnoreError(
                        f"Cannot read {APM_IGNORE_FILENAME} at {ignore_file}: {exc}"
                    ) from exc
                layers.append(
                    _IgnoreLayer(
                        directory=current,
                        rel_prefix=rel_prefix,
                        spec=GitIgnoreSpec.from_lines(text.splitlines()),
                    )
                )
        layers.sort(key=lambda layer: (layer.rel_prefix.count("/"), layer.rel_prefix))
        spec = cls(root, tuple(layers))
        spec.validate_required()
        _LOAD_CACHE[cache_key] = spec
        return spec

    def validate_required(self) -> None:
        """Refuse to ignore ``SKILL.md`` or ``apm.yml`` at the package root."""
        for name in _REQUIRED_ROOT_FILES:
            candidate = self.package_root / name
            try:
                exists = candidate.is_file() and not candidate.is_symlink()
            except OSError:
                exists = False
            if exists and self.is_ignored(candidate, is_dir=False):
                raise ApmIgnoreError(f".apmignore cannot exclude required file {name}")

    def is_ignored(self, path: Path, *, is_dir: bool | None = None) -> bool:
        """Return True when *path* is excluded by the loaded rules."""
        if not self._layers:
            return False
        rel_posix = self._rel_posix(path)
        if rel_posix is None:
            return False
        if is_dir is None:
            try:
                is_dir = path.is_dir() and not path.is_symlink()
            except OSError:
                is_dir = False
        parts = [part for part in rel_posix.split("/") if part]
        for index in range(1, len(parts)):
            ancestor = "/".join(parts[:index])
            if self._match_layers(ancestor, is_dir=True):
                return True
        return self._match_layers(rel_posix, is_dir=bool(is_dir))

    def copytree_ignore(self, *extras: CopyIgnore) -> CopyIgnore:
        """Return a ``shutil.copytree`` ignore callback for this package."""
        from apm_cli.security.gate import ignore_non_content

        extra_callbacks: tuple[CopyIgnore, ...] = (ignore_non_content, *extras)

        def ignore(directory: str, contents: list[str]) -> list[str]:
            dropped: set[str] = set()
            for extra in extra_callbacks:
                dropped.update(extra(directory, contents))
            current = Path(directory)
            for name in contents:
                if name in dropped:
                    continue
                candidate = current / name
                try:
                    is_dir = candidate.is_dir() and not candidate.is_symlink()
                except OSError:
                    is_dir = False
                if self.is_ignored(candidate, is_dir=is_dir):
                    dropped.add(name)
            return list(dropped)

        return ignore

    def _rel_posix(self, path: Path) -> str | None:
        rel = portable_relpath(path, self.package_root)
        if rel in {".", ""}:
            return None
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError):
            resolved = path.absolute()
        try:
            resolved.relative_to(self.package_root)
        except ValueError:
            return None
        return rel

    def _match_layers(self, rel_posix: str, *, is_dir: bool) -> bool:
        ignored = False
        for layer in self._layers:
            layer_rel = _relative_to_layer(rel_posix, layer.rel_prefix)
            if layer_rel is None:
                continue
            decision = _layer_decision(layer.spec, layer_rel, is_dir=is_dir)
            if decision is not None:
                ignored = decision
        return ignored


def _relative_to_layer(rel_posix: str, layer_prefix: str) -> str | None:
    """Return *rel_posix* relative to a nested ignore file, or None."""
    if not layer_prefix:
        return rel_posix
    if rel_posix == layer_prefix:
        return None
    prefix = layer_prefix + "/"
    if not rel_posix.startswith(prefix):
        return None
    return rel_posix[len(prefix) :]


def _layer_decision(spec: GitIgnoreSpec, rel_posix: str, *, is_dir: bool) -> bool | None:
    """Last matching pattern in one file: True ignore, False include, None none."""
    candidates = [rel_posix]
    if is_dir and not rel_posix.endswith("/"):
        candidates.append(rel_posix + "/")
    best_include: bool | None = None
    best_index = -1
    for candidate in candidates:
        result = spec.check_file(candidate)
        if result.include is None or result.index is None:
            continue
        if result.index >= best_index:
            best_include = bool(result.include)
            best_index = result.index
    return best_include


def load_apmignore(package_root: Path) -> ApmIgnoreSpec:
    """Load ``.apmignore`` rules for *package_root*."""
    return ApmIgnoreSpec.load(package_root)


def clear_apmignore_cache() -> None:
    """Drop cached ignore specs. Called from discovery cache invalidation."""
    _LOAD_CACHE.clear()
