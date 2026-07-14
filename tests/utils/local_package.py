"""Source-only local APM package authoring helpers for hermetic tests."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TypeAlias

import yaml

from apm_cli.core.apm_yml import parse_targets_field
from apm_cli.models.dependency import DependencyReference
from apm_cli.models.dependency.object_fields import reject_unknown_fields
from apm_cli.utils.path_security import ensure_path_within, validate_path_segments
from apm_cli.utils.yaml_io import dump_yaml, load_yaml

DependencyInput: TypeAlias = str | Mapping[str, object]
_MANIFEST_LAYOUT = "manifest"
_POLICY_LAYOUT = "policy"
_SKILL_LAYOUT = "skill"
_AGENT_LAYOUT = "agent"
_INSTRUCTION_LAYOUT = "instruction"
_PRIMITIVE_LAYOUTS = frozenset({_SKILL_LAYOUT, _AGENT_LAYOUT, _INSTRUCTION_LAYOUT})
_GIT_DEPENDENCY_FIELDS = {
    "git",
    "type",
    "path",
    "ref",
    "allow_insecure",
    "alias",
    "skills",
    "targets",
}


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
        root.mkdir(parents=True, exist_ok=True)
        self._root = root.resolve()
        self._packages: dict[int, LocalPackage] = {}

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
        ensure_path_within(package_root, self._root)
        validated_dependencies = self._validate_dependencies(dependencies)
        validated_targets = parse_targets_field({"targets": list(targets)}) if targets else []
        package_root.mkdir(parents=True, exist_ok=False)
        manifest_path = self._validated_source_path(
            package_root,
            PurePosixPath("apm.yml"),
            frozenset({_MANIFEST_LAYOUT}),
        )
        manifest: dict[str, object] = {
            "name": name,
            "version": version,
            "description": f"Hermetic test package {name}",
            "author": "APM Test",
        }
        if validated_dependencies:
            manifest["dependencies"] = {"apm": validated_dependencies}
        if validated_targets:
            manifest["targets"] = validated_targets
        dump_yaml(manifest, manifest_path)
        package = LocalPackage(name=name, root=package_root, manifest_path=manifest_path)
        self._packages[id(package)] = package
        return package

    def add_skill(self, package: LocalPackage, name: str, content: str) -> Path:
        """Author a bundled skill and return its source path."""
        self._validate_segment(name, "skill")
        path = self._source_path(
            package,
            PurePosixPath("skills") / name / "SKILL.md",
            frozenset({_SKILL_LAYOUT}),
        )
        return self._write_text(path, content)

    def add_agent(self, package: LocalPackage, name: str, content: str) -> Path:
        """Author an agent primitive and return its source path."""
        self._validate_segment(name, "agent")
        path = self._source_path(
            package,
            PurePosixPath(".apm") / "agents" / f"{name}.agent.md",
            frozenset({_AGENT_LAYOUT}),
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
            frozenset({_INSTRUCTION_LAYOUT}),
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
        parent_manifest_path = self._manifest_path(parent)
        child_root = self._owned_package(child).root
        try:
            manifest = load_yaml(parent_manifest_path)
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid manifest YAML: {parent_manifest_path}") from exc
        if not isinstance(manifest, dict):
            raise ValueError(f"Invalid manifest mapping: {parent_manifest_path}")

        relative_path = (
            Path(os.path.relpath(child_root.resolve(), parent.root.resolve()))
            .as_posix()
            .replace("\\", "/")
        )
        entry: dict[str, object] = {"path": relative_path}
        if alias is not None:
            entry["alias"] = alias
        if skills:
            entry["skills"] = list(skills)
        if targets:
            entry["targets"] = list(targets)
        validated_entry = DependencyReference.parse_from_dict(entry).to_apm_yml_entry()
        if isinstance(validated_entry, str):
            validated_entry = {"path": validated_entry}
        dependency_block = manifest.get("dependencies")
        if dependency_block is None:
            dependency_block = {}
            manifest["dependencies"] = dependency_block
        if not isinstance(dependency_block, dict):
            raise ValueError(f"Invalid dependencies mapping: {parent_manifest_path}")
        dependencies = dependency_block.get("apm")
        if dependencies is None:
            dependencies = []
            dependency_block["apm"] = dependencies
        if not isinstance(dependencies, list):
            raise ValueError(f"Invalid APM dependencies list: {parent_manifest_path}")
        dependencies.append(validated_entry)
        dump_yaml(manifest, parent_manifest_path)

    def add_relative_link(
        self,
        package: LocalPackage,
        link_path: PurePosixPath,
        target_path: PurePosixPath,
        *,
        label: str = "target",
    ) -> Path:
        """Author a Markdown relative link as a package source input."""
        path = self._source_path(package, link_path, _PRIMITIVE_LAYOUTS)
        self._validate_relative_link_target(target_path)
        return self._write_text(path, f"[{label}]({target_path.as_posix()})\n")

    def write_policy(
        self,
        package: LocalPackage,
        policy: Mapping[str, object],
    ) -> Path:
        """Write the package policy source using canonical YAML I/O."""
        path = self._source_path(
            package,
            PurePosixPath("apm-policy.yml"),
            frozenset({_POLICY_LAYOUT}),
        )
        dump_yaml(dict(policy), path)
        return path

    def _source_path(
        self,
        package: LocalPackage,
        relative_path: PurePosixPath,
        allowed_layouts: frozenset[str],
    ) -> Path:
        owned = self._owned_package(package)
        return self._validated_source_path(owned.root, relative_path, allowed_layouts)

    def _manifest_path(self, package: LocalPackage) -> Path:
        owned = self._owned_package(package)
        return self._validated_source_path(
            owned.root,
            PurePosixPath("apm.yml"),
            frozenset({_MANIFEST_LAYOUT}),
        )

    def _owned_package(self, package: LocalPackage) -> LocalPackage:
        if self._packages.get(id(package)) is not package:
            raise ValueError("Local package is not owned by this factory")
        self._validate_segment(package.name, "package")
        if package.root != self._root / package.name:
            raise ValueError("Owned package root does not match its factory path")
        if package.manifest_path != package.root / "apm.yml":
            raise ValueError("Owned package manifest does not match its source layout")
        ensure_path_within(package.root, self._root)
        self._reject_symlink_components(package.root, self._root)
        return package

    def _validated_source_path(
        self,
        package_root: Path,
        relative_path: PurePosixPath,
        allowed_layouts: frozenset[str],
    ) -> Path:
        raw_path = relative_path.as_posix()
        validate_path_segments(raw_path, context="package source path", reject_empty=True)
        if relative_path.is_absolute():
            raise ValueError(f"Unsafe package source path: {relative_path}")
        layout = self._source_layout(relative_path)
        if layout not in allowed_layouts:
            raise ValueError(f"Refusing unsupported package source layout: {relative_path}")
        path = package_root.joinpath(*relative_path.parts)
        ensure_path_within(path, package_root)
        self._reject_symlink_components(path, package_root)
        return path

    @staticmethod
    def _source_layout(relative_path: PurePosixPath) -> str | None:
        parts = relative_path.parts
        if "\\" in relative_path.as_posix() or ".git" in parts:
            return None
        if parts == ("apm.yml",):
            return _MANIFEST_LAYOUT
        if parts == ("apm-policy.yml",):
            return _POLICY_LAYOUT
        if len(parts) >= 3 and parts[0] == "skills":
            return _SKILL_LAYOUT
        if len(parts) != 3:
            return None
        if (
            parts[:2] == (".apm", "agents")
            and parts[2].endswith(".agent.md")
            and parts[2] != ".agent.md"
        ):
            return _AGENT_LAYOUT
        if (
            parts[:2] == (".apm", "instructions")
            and parts[2].endswith(".instructions.md")
            and parts[2] != ".instructions.md"
        ):
            return _INSTRUCTION_LAYOUT
        return None

    @staticmethod
    def _validate_segment(name: str, kind: str) -> None:
        validate_path_segments(name, context=f"{kind} name", reject_empty=True)
        if len(name.replace("\\", "/").split("/")) != 1:
            raise ValueError(f"Unsafe {kind} name: {name!r}")

    @staticmethod
    def _validate_relative_link_target(target_path: PurePosixPath) -> None:
        raw_target = target_path.as_posix()
        if target_path.is_absolute() or "\\" in raw_target or PureWindowsPath(raw_target).drive:
            raise ValueError(f"Markdown target path must be a relative POSIX path: {target_path}")

    @staticmethod
    def _validate_dependencies(
        dependencies: Sequence[DependencyInput],
    ) -> list[str | dict[str, object]]:
        validated: list[str | dict[str, object]] = []
        for entry in dependencies:
            object_entry: dict[str, object] | None = None
            if isinstance(entry, str):
                dependency = DependencyReference.parse(entry)
            elif isinstance(entry, Mapping):
                object_entry = dict(entry)
                dependency = DependencyReference.parse_from_dict(object_entry)
                if "git" in object_entry:
                    reject_unknown_fields(object_entry, _GIT_DEPENDENCY_FIELDS, "git")
            else:
                raise TypeError("APM dependency entries must be strings or mappings")
            serialized = dependency.to_apm_yml_entry()
            if object_entry is not None and isinstance(serialized, str):
                serialized = object_entry
            validated.append(serialized)
        return validated

    @staticmethod
    def _reject_symlink_components(path: Path, base_dir: Path) -> None:
        try:
            relative = path.relative_to(base_dir)
        except ValueError as exc:
            raise ValueError(f"Path is outside package root: {path}") from exc
        current = base_dir
        if current.is_symlink():
            raise ValueError(f"Refusing symlinked package source path: {current}")
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise ValueError(f"Refusing symlinked package source path: {current}")

    @staticmethod
    def _write_text(path: Path, content: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
        return path
