"""Claude-native inline hook admission for already selected standalone primitives."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from apm_cli.hook_contract import HookContractError, hook_handlers
from apm_cli.utils.diagnostics import printable_ascii_text
from apm_cli.utils.paths import portable_relpath
from apm_cli.utils.yaml_io import load_frontmatter, loads_frontmatter, yaml_to_str

if TYPE_CHECKING:
    from apm_cli.install.deployable_source_plan import DeployableSourcePlan
    from apm_cli.integration.targets import TargetProfile
    from apm_cli.utils.diagnostics import DiagnosticCollector

_EVENTS = frozenset(
    {
        "SessionStart",
        "SessionEnd",
        "Setup",
        "UserPromptSubmit",
        "UserPromptExpansion",
        "PreToolUse",
        "PermissionRequest",
        "PermissionDenied",
        "PostToolUse",
        "PostToolUseFailure",
        "PostToolBatch",
        "Notification",
        "MessageDisplay",
        "SubagentStart",
        "SubagentStop",
        "TaskCreated",
        "TaskCompleted",
        "Stop",
        "StopFailure",
        "TeammateIdle",
        "InstructionsLoaded",
        "ConfigChange",
        "CwdChanged",
        "DirectoryAdded",
        "FileChanged",
        "WorktreeCreate",
        "WorktreeRemove",
        "PreCompact",
        "PostCompact",
        "PreModelSwitch",
        "PostModelSwitch",
        "Elicitation",
        "ElicitationResult",
    }
)
_COMMON_FIELDS = {"type", "if", "timeout", "statusMessage", "once"}
_HANDLER_FIELDS = {
    "command": {"command", "async", "asyncRewake", "shell", "onFailure"},
    "http": {"url", "headers", "allowedEnvVars", "onFailure"},
    "prompt": {"prompt", "model"},
    "agent": {"prompt", "model"},
    "mcp_tool": {"server", "tool", "input"},
}
_REQUIRED_FIELDS = {
    "command": ("command",),
    "http": ("url",),
    "prompt": ("prompt",),
    "agent": ("prompt",),
    "mcp_tool": ("server", "tool"),
}


def read_inline_hook_document(source: Path) -> dict[str, Any] | None:
    """Validate a selected primitive's hooks without interpreting its Markdown body."""
    try:
        metadata = load_frontmatter(source).metadata
        if "hooks" not in metadata:
            return None
        document = {"hooks": metadata["hooks"]}
        declarations = hook_handlers(document)
        for declaration in declarations:
            value = declaration.value
            if declaration.event not in _EVENTS:
                raise HookContractError(f"unsupported Claude event {declaration.event!r}")
            if declaration.json_pointer.count("/") == 3:
                if set(value) - {"matcher", "hooks"} or not isinstance(value.get("hooks"), list):
                    raise HookContractError("Claude inline hooks require nested matcher groups")
                if "matcher" in value and not isinstance(value["matcher"], str):
                    raise HookContractError("matcher must be a string")
                continue
            handler_type = value.get("type")
            if not isinstance(handler_type, str) or handler_type not in _HANDLER_FIELDS:
                raise HookContractError(f"unsupported Claude handler type {handler_type!r}")
            unknown = set(value) - _COMMON_FIELDS - _HANDLER_FIELDS[handler_type]
            if unknown:
                raise HookContractError(
                    f"unsupported Claude inline handler fields: {', '.join(sorted(unknown))}"
                )
            for key in _REQUIRED_FIELDS[handler_type]:
                if not isinstance(value.get(key), str) or not value[key].strip():
                    raise HookContractError(f"{handler_type} handler requires a nonempty {key}")
            for key in ("once", "async", "asyncRewake"):
                if key in value and not isinstance(value[key], bool):
                    raise HookContractError(f"{key} must be a boolean")
            for key in ("if", "statusMessage", "model"):
                if key in value and not isinstance(value[key], str):
                    raise HookContractError(f"{key} must be a string")
            if "timeout" in value and (
                isinstance(value["timeout"], bool)
                or not isinstance(value["timeout"], (int, float))
                or value["timeout"] <= 0
            ):
                raise HookContractError("timeout must be a positive number")
            if "shell" in value and value["shell"] not in {"bash", "powershell"}:
                raise HookContractError("shell must be bash or powershell")
            if "onFailure" in value and value["onFailure"] not in {"continue", "block"}:
                raise HookContractError("onFailure must be continue or block")
        return document if declarations else None
    except (yaml.YAMLError, HookContractError) as exc:
        raise HookContractError(
            f"Cannot deploy Claude inline hooks in {printable_ascii_text(str(source))}: "
            f"{printable_ascii_text(str(exc))}. "
            "Use a supported structured hook definition and reinstall; "
            "APM will not install the primitive without its hooks."
        ) from exc


def render_inline_hook_text(
    content: str, source: Path, source_plan: DeployableSourcePlan | None
) -> str:
    """Replace only the hook payload with its authorized native path projection."""
    document = source_plan.inline_rendered_documents.get(source) if source_plan else None
    if document is None:
        return content
    post = loads_frontmatter(content, preserve_body=True)
    post.metadata["hooks"] = document["hooks"]
    return f"---\n{yaml_to_str(post.metadata)}---\n{post.content}"


def materialize_inline_hook_resources(
    source_plan: DeployableSourcePlan,
    package_info: Any,
    project_root: Path,
    *,
    user_scope: bool,
    managed_files: set[str] | None,
    force: bool,
    diagnostics: DiagnosticCollector | None,
    recorded_hashes: Mapping[str, str] | None = None,
    targets: Iterable[TargetProfile] = (),
    command_deploy_root: Path | None = None,
) -> tuple[DeployableSourcePlan, list[Path]]:
    """Use the shared hook bundle owner before activating any inline primitive."""
    from apm_cli.install.exec_gate import resolve_package_key
    from apm_cli.integration.hook_bundle import copy_deployed_hook_bundle
    from apm_cli.integration.hook_integrator import HookIntegrator

    selection = source_plan.hook_source_selection
    if selection is None:
        return source_plan, []
    documents = selection.inline_for("claude")
    if not documents:
        return source_plan, []
    target = next((target for target in targets if target.name == "claude"), None)
    if target is None:
        raise HookContractError("Claude inline hooks require an admitted Claude target.")
    integrator = HookIntegrator()
    identity = resolve_package_key(package_info, "")
    rendered = {}
    target_paths: list[Path] = []
    for source, document in documents.items():
        relative = portable_relpath(source, source_plan.source_root)
        namespace = hashlib.sha256(f"{identity}\0{relative}".encode()).hexdigest()
        rewritten, scripts = integrator._rewrite_hooks_data(
            document,
            source_plan.source_root,
            f"inline-{namespace}",
            "claude",
            hook_file_dir=source.parent,
            root_dir=target.root_dir,
            deploy_root=command_deploy_root or (project_root if user_scope else None),
        )
        copy_deployed_hook_bundle(
            integrator,
            package_path=source_plan.source_root,
            hook_file_dir=source.parent,
            project_root=project_root,
            scripts=scripts,
            managed_files=managed_files,
            force=force,
            diagnostics=diagnostics,
            target_paths=target_paths,
            hook_descriptor_files=set(selection.descriptor_files.get("claude", ())),
            source_plan=source_plan,
            selected_bundle_files=selection.bundle_for("claude"),
            require_complete=True,
            recorded_hashes=recorded_hashes,
        )
        rendered[source] = rewritten
    return replace(source_plan, inline_rendered_documents=rendered), target_paths
