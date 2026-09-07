"""``--apply``: stage converted files, validate, commit atomically, update apm.yml."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import click

from apm_cli.constants import APM_YML_FILENAME
from apm_cli.core.command_logger import CommandLogger
from apm_cli.core.scope import USER_APM_DIR
from apm_cli.core.target_detection import manifest_targets_from_target_option
from apm_cli.hook_contract import HookContractError, parse_hook_source
from apm_cli.integration.skill_integrator import normalize_skill_name
from apm_cli.primitives.parser import parse_primitive_file, parse_skill_file
from apm_cli.utils.path_security import ensure_path_within, safe_rmtree

from .converters import (
    CONVERTERS,
    ConvertContext,
    ConvertError,
    ConvertResult,
    register_builtin_converters,
    summarize_changes,
)
from .converters.base import NameAllocator, content_key, flatten_relative
from .manifest_edit import apply_manifest_delta
from .model import AdoptionReport, Finding, HarnessKind, Importability, Ownership, Scope
from .provenance import ImportSources, hash_source
from .redact import Redactor
from .render import render

_SUFFIXES = {
    HarnessKind.INSTRUCTION: (
        "instructions",
        ".instructions.md",
        (".instructions.md", ".md", ".mdc"),
    ),
    HarnessKind.RULE: ("instructions", ".instructions.md", (".instructions.md", ".md", ".mdc")),
    HarnessKind.AGENT: ("agents", ".agent.md", (".agent.md", ".md", ".toml")),
    HarnessKind.PROMPT: ("prompts", ".prompt.md", (".prompt.md", ".md")),
    HarnessKind.COMMAND: ("prompts", ".prompt.md", (".prompt.md", ".md", ".toml")),
}
_DEFAULT_MCP_TOOL_ORDER: tuple[str, ...] = (
    "claude",
    "copilot",
    "vscode",
    "cursor",
    "gemini",
    "kiro",
    "codex",
    "opencode",
    "windsurf",
    "antigravity",
)
_VALIDATION_ADVISORIES = ("instruction will apply globally", "Missing 'description'")


@dataclass
class WriteItem:
    finding: Finding
    converter_id: str
    dest_rel: str
    decision: str
    source_hash: str | None
    result: ConvertResult | None = None
    error: str | None = None


@dataclass
class WritePlan:
    apm_dir: Path
    items: list[WriteItem] = field(default_factory=list)
    skipped: list[tuple[Finding, str]] = field(default_factory=list)

    @property
    def to_write(self) -> list[WriteItem]:
        return [i for i in self.items if i.decision in ("write", "refresh")]


def _source_rel(finding: Finding) -> PurePosixPath:
    path = PurePosixPath(finding.display_path)
    parts = list(path.parts)
    # Drop the harness root and primitive subdir so nested names flatten below them.
    return PurePosixPath(*parts[2:]) if len(parts) > 2 else PurePosixPath(path.name)


def plan_write(
    report: AdoptionReport, apm_dir: Path, provenance: ImportSources, allocator: NameAllocator
) -> WritePlan:
    """Decide destinations for every eligible finding without touching the disk."""
    plan = WritePlan(apm_dir=apm_dir)
    for finding in report.findings:
        if finding.importability not in (Importability.APM_NATIVE, Importability.CONVERTIBLE):
            continue
        if finding.ownership is not Ownership.HOST_OWNED:
            plan.skipped.append((finding, f"{finding.ownership.value}; not written"))
            continue
        converter = CONVERTERS.get(finding.converter_id)
        if converter is None:
            plan.skipped.append((finding, f"no converter for {finding.converter_id}"))
            continue
        if finding.kind is HarnessKind.MCP_SERVER:
            plan.items.append(
                WriteItem(finding, converter.id, "apm.yml#dependencies.mcp", "write", None)
            )
            continue
        source_hash = hash_source(finding.abs_path)
        if finding.abs_path is not None and finding.kind is not HarnessKind.SKILL:
            holder = allocator.claim_content(content_key(finding.abs_path), finding.display_path)
            if holder is not None and holder != finding.display_path:
                plan.skipped.append((finding, f"identical content already imported from {holder}"))
                continue
        try:
            dest_rel, renamed = _destination(finding, allocator)
        except ConvertError as exc:
            plan.skipped.append((finding, str(exc)))
            continue
        decision = provenance.decide(dest_rel, apm_dir / dest_rel, source_hash)
        item = WriteItem(finding, converter.id, dest_rel, decision, source_hash)
        if renamed:
            item.error = None
        plan.items.append(item)
    return plan


def _destination(finding: Finding, allocator: NameAllocator) -> tuple[str, bool]:
    if finding.kind is HarnessKind.ROOT_CONTEXT:
        nested = _source_rel(finding).parent if "/" in finding.display_path else PurePosixPath()
        stem = (
            f"{finding.tool}-root"
            if str(nested) in ("", ".")
            else f"{nested.as_posix()}-{finding.tool}-root"
        )
        return allocator.allocate("instructions", stem, ".instructions.md", finding.tool)
    if finding.kind is HarnessKind.SKILL:
        folder = normalize_skill_name(PurePosixPath(finding.display_path).name)
        return allocator.allocate("skills", folder, "", finding.tool)
    if finding.kind is HarnessKind.HOOK:
        stem = f"{finding.tool}-native"
        if finding.payload is None and finding.abs_path is not None:
            stem = f"{finding.tool}-{PurePosixPath(finding.display_path).stem}-native"
        return allocator.allocate("hooks", stem, ".json", finding.tool)
    entry = _SUFFIXES.get(finding.kind)
    if entry is None:
        raise ConvertError(f"no destination rule for {finding.kind.value}")
    subdir, suffix, strip = entry
    stem, _nested = flatten_relative(_source_rel(finding), strip)
    return allocator.allocate(subdir, stem, suffix, finding.tool)


def stage(
    plan: WritePlan, staging_apm: Path, ctx: ConvertContext
) -> list[tuple[str, Mapping[str, Any]]]:
    """Run converters into the staging directory; collect (tool, manifest fragment) pairs."""
    fragments: list[tuple[str, Mapping[str, Any]]] = []
    for item in plan.to_write:
        converter = CONVERTERS.get(item.converter_id)
        if converter is None:
            item.error = "converter missing"
            continue
        dest = (
            staging_apm / item.dest_rel
            if item.finding.kind is not HarnessKind.MCP_SERVER
            else staging_apm
        )
        try:
            ensure_path_within(dest, staging_apm)
            item.result = converter.convert(item.finding, dest, ctx=ctx)
            if item.result.manifest_fragment:
                fragments.append((item.finding.tool, item.result.manifest_fragment))
        except ConvertError as exc:
            item.error = str(exc)
        except Exception as exc:  # converter bug must not leak a traceback with paths
            item.error = f"converter failed ({type(exc).__name__})"
    return fragments


def validate_staged(staging_apm: Path) -> list[str]:
    """Parse every staged primitive with the same parsers apm install uses."""
    errors: list[str] = []
    for path in sorted(staging_apm.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(staging_apm).as_posix()
        try:
            if path.name == "SKILL.md":
                problems = parse_skill_file(path).validate()
            elif path.suffix == ".json" and rel.startswith("hooks/") and "/scripts/" not in rel:
                parse_hook_source(json.loads(path.read_text(encoding="utf-8")))
                problems = []
            elif rel.startswith(("instructions/", "agents/", "prompts/")) and path.suffix == ".md":
                problems = (
                    parse_primitive_file(path).validate() if not rel.startswith("prompts/") else []
                )
            else:
                problems = []
        except (HookContractError, ValueError, OSError) as exc:
            problems = [f"unparseable ({type(exc).__name__})"]
        except Exception as exc:
            problems = [f"unparseable ({type(exc).__name__})"]
        for problem in problems:
            if any(advisory in problem for advisory in _VALIDATION_ADVISORIES):
                continue
            errors.append(f"{rel}: {problem}")
    return errors


def _staged_entries(items: list[WriteItem], staging_apm: Path) -> list[str]:
    """Destinations plus any extra staged files (e.g. copied hook scripts)."""
    rels: list[str] = []
    for item in items:
        if item.result is None or item.finding.kind is HarnessKind.MCP_SERVER:
            continue
        rels.append(item.dest_rel)
        for written in item.result.written:
            try:
                rel = written.relative_to(staging_apm).as_posix()
            except ValueError:
                continue
            if rel != item.dest_rel and not rel.startswith(item.dest_rel + "/") and rel not in rels:
                rels.append(rel)
    return rels


def commit(staging_apm: Path, apm_dir: Path, rels: list[str]) -> list[str]:
    """Move staged entries into place; roll back on the first failure."""
    committed: list[str] = []
    try:
        for rel in rels:
            source = staging_apm / rel
            if not source.exists():
                continue
            target = ensure_path_within(apm_dir / rel, apm_dir)
            if target.exists():
                if (
                    target.is_file()
                    and source.is_file()
                    and target.read_bytes() == source.read_bytes()
                ):
                    continue  # identical shared file (e.g. a hook script) already present
                raise FileExistsError(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
            committed.append(rel)
    except Exception:
        for rel in committed:
            path = apm_dir / rel
            if path.is_dir():
                safe_rmtree(path, apm_dir)
            else:
                path.unlink(missing_ok=True)
        raise
    return committed


def _dedupe_mcp(
    fragments: list[tuple[str, Mapping[str, Any]]], preferred: tuple[str, ...]
) -> tuple[list[dict], list[str]]:
    """Merge MCP fragments by server name.

    Fragments are ordered by *preferred* (the ``--target`` order, then the
    default client order); the first definition of a name wins, identical
    cores merge their ``extra`` blocks, and divergent cores are reported.
    """
    order = {tool: index for index, tool in enumerate([*preferred, *_DEFAULT_MCP_TOOL_ORDER])}
    ranked = sorted(
        enumerate(fragments), key=lambda item: (order.get(item[1][0], len(order)), item[0])
    )
    notes: list[str] = []
    by_name: dict[str, dict] = {}
    owner: dict[str, str] = {}
    for _index, (tool, fragment) in ranked:
        for entry in fragment.get("dependencies", {}).get("mcp", []):
            name = str(entry.get("name"))
            if name not in by_name:
                by_name[name] = dict(entry)
                owner[name] = tool
                continue
            current = by_name[name]
            core = {k: v for k, v in entry.items() if k != "extra"}
            existing_core = {k: v for k, v in current.items() if k != "extra"}
            if core == existing_core:
                merged_extra = {**(entry.get("extra") or {}), **(current.get("extra") or {})}
                if merged_extra:
                    current["extra"] = merged_extra
                continue
            differing = sorted(
                k for k in set(core) | set(existing_core) if core.get(k) != existing_core.get(k)
            )
            notes.append(
                f"dependencies.mcp: divergent definitions for {name}; kept {owner[name]} over {tool} "
                f"(fields: {', '.join(differing)})"
            )
    return list(by_name.values()), notes


def _describe_plan(
    plan: WritePlan,
    to_write: list[WriteItem],
    manifest_notes: list[str],
    validation_problems: list[str],
    *,
    logger: CommandLogger,
    apm_display: str,
    manifest_name: str,
) -> None:
    """Print everything the user is about to approve: files, losses, skips, manifest edits."""
    file_items = [
        i for i in to_write if i.finding.kind is not HarnessKind.MCP_SERVER and not i.error
    ]
    mcp_items = [i for i in to_write if i.finding.kind is HarnessKind.MCP_SERVER and not i.error]
    failures = [i for i in to_write if i.error]
    click.echo()
    logger.info("Migration plan")
    if file_items:
        logger.info(f"Will write {len(file_items)} file(s) into {apm_display}/:")
        for item in file_items:
            summary = summarize_changes(item.result.changes) if item.result else ""
            logger.tree_item(
                f"{item.finding.display_path} -> {item.dest_rel} ({item.decision}) [{summary}]"
            )
            for change in item.result.changes if item.result else ():
                if change.severity == "warning":
                    logger.tree_item(f"    {change.path}: {change.reason}")
    if mcp_items:
        logger.info(f"Will add {len(mcp_items)} MCP server(s) to {manifest_name}:")
        for item in mcp_items:
            logger.tree_item(item.finding.display_path)
    if manifest_notes:
        logger.info(f"{manifest_name} changes:")
        for note in manifest_notes:
            logger.tree_item(note)
    unchanged = [i for i in plan.items if i.decision not in ("write", "refresh")]
    if unchanged or plan.skipped:
        logger.info("Not written:")
        for item in unchanged:
            logger.tree_item(f"{item.finding.display_path}: {item.decision}")
        for finding, reason in plan.skipped:
            logger.tree_item(f"{finding.display_path}: {reason}")
    if failures:
        logger.warning(f"{len(failures)} item(s) cannot be imported and will be left out:")
        for item in failures:
            logger.tree_item(f"{item.finding.display_path}: {item.error}")
    if validation_problems:
        logger.error("Staged files failed validation; nothing can be written:")
        for problem in validation_problems[:20]:
            logger.tree_item(problem)


def _rollback(
    apm_dir: Path, committed: list[str], manifest: Path, manifest_before: bytes | None
) -> None:
    """Undo a partially applied import so files, provenance and apm.yml stay consistent."""
    for rel in committed:
        path = apm_dir / rel
        if path.is_dir():
            safe_rmtree(path, apm_dir)
        else:
            path.unlink(missing_ok=True)
    if manifest_before is None:
        manifest.unlink(missing_ok=True)
    else:
        manifest.write_bytes(manifest_before)


def run_write(
    report: AdoptionReport,
    *,
    root: Path,
    scope: Scope,
    fmt: str,
    logger: CommandLogger,
    yes: bool,
    target_flag: object | None,
    include_hook_scripts: bool,
) -> int:
    """Full ``--apply`` flow; returns a process exit code.

    Order of operations: convert into a temporary stage and validate, show the
    complete plan (files with their conversion losses, MCP entries, manifest
    edits, skipped and failed items), ask for confirmation, then commit files,
    manifest and provenance together, rolling all three back on any error.
    Exit status is 0 only for a complete import; a partial import exits 1.
    """
    register_builtin_converters()
    apm_dir = root / ".apm" if scope is Scope.PROJECT else root / USER_APM_DIR
    manifest = (root if scope is Scope.PROJECT else apm_dir) / APM_YML_FILENAME
    provenance = ImportSources.load(apm_dir)
    allocator = NameAllocator(apm_dir)
    plan = plan_write(report, apm_dir, provenance, allocator)
    to_write = plan.to_write
    redactor = Redactor(root)
    apm_display = redactor.path(apm_dir, scope)
    ctx = ConvertContext(
        project_root=root,
        scope=scope,
        redactor=redactor,
        include_hook_scripts=include_hook_scripts,
        preferred_tools=tuple(manifest_targets_from_target_option(target_flag) or ()),
    )
    targets = list(dict.fromkeys([*ctx.preferred_tools, *report.proposed_targets]))

    if fmt == "text":
        render(report, "text", logger)
    if not to_write and not targets:
        logger.info("Nothing to write.")
        return 0

    root.mkdir(parents=True, exist_ok=True)
    staging_root = Path(tempfile.mkdtemp(prefix=".apm-adopt-", dir=root))
    staging_apm = staging_root / ".apm"
    staging_apm.mkdir()
    written: list[str] = []
    manifest_notes: list[str] = []
    status = "complete"
    try:
        fragments = stage(plan, staging_apm, ctx)
        problems = validate_staged(staging_apm)
        failures = [i for i in to_write if i.error]
        mcp_entries, mcp_notes = _dedupe_mcp(fragments, ctx.preferred_tools)
        create_config: dict[str, Any] | None = None
        if not manifest.is_file():
            from apm_cli.commands._helpers import _get_default_config

            create_config = _get_default_config(root.name if scope is Scope.PROJECT else "user")
            if targets:
                create_config["targets"] = targets
        preview_notes = mcp_notes + apply_manifest_delta(
            manifest,
            targets=targets,
            mcp_entries=mcp_entries,
            create_config=create_config,
            dry_run=True,
        )
        if fmt == "text":
            _describe_plan(
                plan,
                to_write,
                preview_notes,
                problems,
                logger=logger,
                apm_display=apm_display,
                manifest_name=manifest.name,
            )
        if problems:
            return 1
        importable = [i for i in to_write if not i.error]
        if not importable and not targets:
            logger.error("Nothing could be imported.")
            return 1
        if not yes:
            if not sys.stdin.isatty():
                logger.error("Non-interactive shell: pass --yes to apply")
                return 1
            prompt = f"Apply {len(importable)} change(s) and update {manifest.name}?"
            if failures:
                prompt = f"Apply {len(importable)} change(s), leave out {len(failures)} failed, and update {manifest.name}?"
            if not click.confirm(prompt, default=False):
                logger.info("Cancelled; nothing written.")
                return 0

        manifest_before = manifest.read_bytes() if manifest.is_file() else None
        rels = _staged_entries(to_write, staging_apm)
        try:
            if rels:
                apm_dir.mkdir(parents=True, exist_ok=True)
            written = commit(staging_apm, apm_dir, rels)
            manifest_notes = mcp_notes + apply_manifest_delta(
                manifest,
                targets=targets,
                mcp_entries=mcp_entries,
                create_config=create_config,
            )
            for item in to_write:
                if (
                    item.result is None
                    or item.finding.kind is HarnessKind.MCP_SERVER
                    or item.dest_rel not in written
                ):
                    continue
                provenance.record(
                    item.dest_rel,
                    source=item.finding.display_path,
                    scope=scope.value,
                    converter=item.converter_id,
                    source_hash=item.source_hash or "",
                    dest_abs=apm_dir / item.dest_rel,
                )
            if written:
                provenance.save()
        except Exception as exc:
            _rollback(apm_dir, written, manifest, manifest_before)
            logger.error(f"Import failed ({type(exc).__name__}); all changes were rolled back")
            written = []
            status = "failed"
            return 1
        status = "partial" if failures else "complete"
    finally:
        if staging_root.exists():
            safe_rmtree(staging_root, root)

    failures = [i for i in to_write if i.error]
    if fmt != "text":
        payload = report.to_dict()
        payload["write"] = {
            "status": status,
            "written": written,
            "failed": [{"path": i.finding.display_path, "reason": i.error} for i in failures],
            "skipped": [{"path": f.display_path, "reason": r} for f, r in plan.skipped],
            "manifest": manifest_notes,
            "changes": {
                i.dest_rel: [c.to_dict() for c in i.result.changes]
                for i in to_write
                if i.result is not None
            },
        }
        click.echo(
            json.dumps(payload, indent=2, sort_keys=True) if fmt == "json" else _yaml(payload)
        )
    else:
        click.echo()
        logger.success(f"Wrote {len(written)} file(s) into {apm_display}/", symbol="check")
        for note in manifest_notes:
            logger.tree_item(note)
        if failures:
            logger.warning(
                f"PARTIAL: {len(failures)} item(s) were not imported (listed above); exit status 1"
            )
        _next_steps(logger, report, redactor, scope)
    return 1 if failures else 0


def _yaml(payload: dict) -> str:
    from apm_cli.utils.yaml_io import yaml_to_str

    return yaml_to_str(payload, sort_keys=True)


def _next_steps(
    logger: CommandLogger, report: AdoptionReport, redactor: Redactor, scope: Scope
) -> None:
    click.echo()
    logger.info("Next steps:")
    flag = " --global" if scope is Scope.USER else ""
    logger.tree_item(f"apm install{flag}                 # deploy .apm/ to the detected targets")
    logger.tree_item(
        f"apm install{flag} --target cursor # replay the same context on another harness"
    )
    logger.tree_item("Originals were not modified. Remove them once the APM copies are verified.")
    if any(
        f.kind is HarnessKind.ROOT_CONTEXT and f.importability is Importability.CONVERTIBLE
        for f in report.findings
    ):
        logger.warning(
            "apm compile regenerates CLAUDE.md/AGENTS.md/GEMINI.md from .apm/instructions and will "
            "overwrite hand-authored root files. Commit first, then delete the originals or keep "
            "AGENTS.md hand-authored with <!-- apm:start -->/<!-- apm:end --> markers."
        )
    if any(f.kind is HarnessKind.MCP_SERVER for f in report.findings):
        logger.tree_item(
            "Export any ${PLACEHOLDER} variables listed above before running apm install."
        )
