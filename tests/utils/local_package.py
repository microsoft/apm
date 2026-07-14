"""Source-only local APM package authoring helpers for hermetic tests."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TypeAlias

from apm_cli.utils.yaml_io import dump_yaml, load_yaml

DependencyInput: TypeAlias = str | Mapping[str, object]
_GENERATED_TOP_LEVEL = frozenset({"apm.lock.yaml", "apm_modules", "build", "dist"})


@dataclass(frozen=True)
class LocalPackage:
    """Paths identifying an authored local package."""

    name: str
    root: Path
    manifest_path: Path


class LocalPackageFactory:
    """Author realistic package source inputs without product-generated output."""

    def __init__(self, root: Path) -> None:
        """Create a factory rooted at the package source directory."""
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        name: str,
        *,
        version: str = "0.1.0",
        dependencies: Sequence[DependencyInput] = (),
        targets: Sequence[str] = (),
    ) -> LocalPackage:
        """Create a package source directory and its manifest."""
        self._validate_segment(name, "package")

        package_root = self._root / name
        package_root.mkdir(parents=True, exist_ok=False)
        manifest_path = package_root / "apm.yml"
        manifest: dict[str, object] = {
            "name": name,
            "version": version,
            "description": f"Hermetic test package {name}",
            "author": "APM Test",
        }
        if dependencies:
            manifest["dependencies"] = {"apm": list(dependencies)}
        if targets:
            manifest["targets"] = list(targets)
        dump_yaml(manifest, manifest_path)
        return LocalPackage(name=name, root=package_root, manifest_path=manifest_path)

    def add_skill(self, package: LocalPackage, name: str, content: str) -> Path:
        """Author a bundled skill and return its source path."""
        self._validate_segment(name, "skill")
        path = self._source_path(
            package,
            PurePosixPath("skills") / name / "SKILL.md",
        )
        return self._write_text(path, content)

    def add_agent(self, package: LocalPackage, name: str, content: str) -> Path:
        """Author an agent primitive and return its source path."""
        self._validate_segment(name, "agent")
        path = self._source_path(
            package,
            PurePosixPath(".apm") / "agents" / f"{name}.agent.md",
        )
        return self._write_text(path, content)

    def add_instruction(
        self,
        package: LocalPackage,
        name: str,
        content: str,
    ) -> Path:
        """Author an instruction primitive and return its source path."""
        self._validate_segment(name, "instruction")
        path = self._source_path(
            package,
            PurePosixPath(".apm") / "instructions" / f"{name}.instructions.md",
        )
        return self._write_text(path, content)

    def add_relative_dependency(
        self,
        parent: LocalPackage,
        child: LocalPackage,
        *,
        alias: str | None = None,
        skills: Sequence[str] = (),
        targets: Sequence[str] = (),
    ) -> None:
        """Add a portable sibling-relative dependency to the parent manifest."""
        manifest = load_yaml(parent.manifest_path)
        if manifest is None:
            raise ValueError(f"Empty manifest: {parent.manifest_path}")

        dependencies = manifest.setdefault("dependencies", {}).setdefault("apm", [])
        relative_path = Path(
            os.path.relpath(child.root.resolve(), parent.root.resolve())
        ).as_posix()
        entry: dict[str, object] = {"path": relative_path}
        if alias is not None:
            entry["alias"] = alias
        if skills:
            entry["skills"] = list(skills)
        if targets:
            entry["targets"] = list(targets)
        dependencies.append(entry)
        dump_yaml(manifest, parent.manifest_path)

    def add_relative_link(
        self,
        package: LocalPackage,
        link_path: PurePosixPath,
        target_path: PurePosixPath,
        *,
        label: str = "target",
    ) -> Path:
        """Author a Markdown relative link as a package source input."""
        path = self._source_path(package, link_path)
        return self._write_text(path, f"[{label}]({target_path.as_posix()})\n")

    def write_policy(
        self,
        package: LocalPackage,
        policy: Mapping[str, object],
    ) -> Path:
        """Write the package policy source using canonical YAML I/O."""
        path = package.root / "apm-policy.yml"
        dump_yaml(dict(policy), path)
        return path

    def _source_path(
        self,
        package: LocalPackage,
        relative_path: PurePosixPath,
    ) -> Path:
        if not relative_path.parts or relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError(f"Unsafe source path: {relative_path}")
        if relative_path.parts[0] in _GENERATED_TOP_LEVEL:
            raise ValueError(f"Refusing product-generated path: {relative_path}")
        if relative_path.parts[:2] == (".apm", "cache"):
            raise ValueError(f"Refusing product-generated path: {relative_path}")
        return package.root.joinpath(*relative_path.parts)

    @staticmethod
    def _validate_segment(name: str, kind: str) -> None:
        if not name or "/" in name or "\\" in name or name in {".", ".."}:
            raise ValueError(f"Unsafe {kind} name: {name!r}")

    @staticmethod
    def _write_text(path: Path, content: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
        return path
