# pylint: disable=duplicate-code
"""CommandIntegrator -- orchestrates prompt-to-command integration.

Integrates ``.prompt.md`` files as commands for any target that supports the
``commands`` primitive (e.g. ``.claude/commands/``, ``.opencode/commands/``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import frontmatter

from apm_cli.integration.base_integrator import BaseIntegrator, IntegrationResult
from apm_cli.security.gate import BLOCK_POLICY, SecurityGate

from .._opts import IntegrateOpts, SyncRemoveOpts
from ._cmd_helpers import (
    _build_command_target_path,
    _check_passthrough_notice,
    _collect_command_security_messages,
    _command_base_name,
    _CommandTargetContext,
    _emit_command_warnings,
    _integrate_prompt_file,
)
from ._input_helpers import _PRESERVED_COMMAND_KEYS_DISPLAY
from ._legacy import _LegacyCommandsMixin
from ._transform import _transform_prompt_to_command
from ._transform import _write_gemini_command as _write_gemini_command_fn

if TYPE_CHECKING:
    from apm_cli.integration.targets import TargetProfile
    from apm_cli.utils.diagnostics import DiagnosticCollector

logger = logging.getLogger(__name__)

# Re-export for backward compat (tests import CommandIntegrationResult).
CommandIntegrationResult = IntegrationResult


class CommandIntegrator(BaseIntegrator, _LegacyCommandsMixin):
    """Handles integration of APM package prompts into .claude/commands/.

    Transforms .prompt.md files into Claude Code custom slash commands
    during package installation, following the same pattern as PromptIntegrator.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Track which (target_name) values have already received the
        # one-shot Claude-frontmatter-passthrough notice so the message
        # fires once per (install run, target), not once per package or
        # once per file.  Reset implicitly when a new integrator is
        # constructed (one per install run).
        self._passthrough_notified: set[str] = set()

    def _should_emit_passthrough_notice(
        self,
        target_name: str,
        format_id: str,
        *,
        had_dropped_keys: bool,
    ) -> bool:
        return _check_passthrough_notice(
            target_name,
            format_id,
            had_dropped_keys=had_dropped_keys,
            notified=self._passthrough_notified,
        )

    def find_prompt_files(self, package_path: Path) -> list[Path]:
        """Find all .prompt.md files in a package."""
        return self.find_files_by_glob(package_path, "*.prompt.md", subdirs=[".apm/prompts"])

    def integrate_command(
        self,
        source: Path,
        target: Path,
        package_info: Any,
        original_path_or_opts=None,
        **legacy_kwargs,
    ) -> tuple[int, bool, bool]:
        """Integrate a prompt file as a slash command (verbatim copy with format conversion).

        Deploys to ``.claude/commands/`` (Claude Code), ``.cursor/commands/``
        (Cursor), or any other target whose ``commands`` primitive uses the
        shared ``claude_command`` format_id.  ``target_name`` is woven into
        diagnostic messages so users can tell which IDE the command was
        installed for.

        TODO(cursor-command-format): track via dedicated follow-up issue
        once filed.  Cursor currently reuses the ``claude_command``
        transformer which preserves only a common subset of frontmatter
        (description, allowed-tools, model, argument-hint, input).  When a
        dedicated ``cursor_command`` transformer lands, the target
        dispatch in ``integrate_commands_for_target`` should branch to
        it.  Dropped keys are surfaced via diagnostics.warn() per file
        in the meantime.

        Args:
            source: Source .prompt.md file path.
            target: Target command file path (e.g. .claude/commands/foo.md
                    or .cursor/commands/foo.md).
            package_info: PackageInfo object with package metadata.
            original_path_or_opts: Original source path (legacy API) or
                IntegrateOpts (new API). Ignored when it is a Path instance.
            target_name: Name of the deployment target (e.g. ``"claude"``,
                ``"cursor"``, ``"opencode"``) so diagnostic messages stay
                target-agnostic instead of always saying "Claude".

        Returns:
            tuple[int, bool, bool]: (links_resolved, written, had_dropped_keys).
            ``written`` is False when a critical post-transform security
            finding causes the write to be skipped (defence-in-depth on
            top of the pre-install BLOCK gate).  ``had_dropped_keys`` is
            True when the source frontmatter carried at least one key
            outside the cross-tool subset preserved by the shared
            ``claude_command`` transformer; the dispatcher uses this to
            decide whether to surface the one-shot passthrough notice.
        """
        _diagnostics = legacy_kwargs.get("diagnostics")
        target_name: str = legacy_kwargs.get("target_name", "claude")
        if isinstance(original_path_or_opts, IntegrateOpts):
            opts = original_path_or_opts
        else:
            opts = IntegrateOpts(diagnostics=_diagnostics)
        diagnostics = opts.diagnostics

        # Transform to command format.
        command_name, post, warnings, dropped_keys = _transform_prompt_to_command(source)

        # Resolve context links in content.
        post.content, links_resolved = self.resolve_links(post.content, source, target)

        pkg_name = getattr(
            getattr(package_info, "package", None),
            "name",
            "",
        )

        # Surface dropped (lossy-transform) frontmatter keys.  The shared
        # claude_command transformer preserves only a common subset of
        # frontmatter; any other source key is silently discarded by the
        # transformer.  Warn so users see the lossy transform at install
        # time -- core "install adds, never silently mutates" contract.
        if dropped_keys and diagnostics is not None:
            preserved_list = ", ".join(sorted(_PRESERVED_COMMAND_KEYS_DISPLAY))
            diagnostics.warn(
                message=(
                    f"{target_name.capitalize()} command {command_name}: "
                    f"frontmatter keys not supported for {target_name} commands "
                    f"and were dropped: {', '.join(dropped_keys)}. "
                    f"Supported keys: {preserved_list}."
                ),
                package=pkg_name,
            )

        # Surface install-time info when input -> arguments mapping happened so
        # users are not surprised by content that differs from the source package.
        mapped_args = post.metadata.get("arguments") if post.metadata else None
        if mapped_args and diagnostics is not None:
            diagnostics.info(
                message=(
                    f"Mapped input -> command arguments in {target.name}: "
                    f"[{', '.join(mapped_args)}]"
                ),
                package=pkg_name,
                detail=(
                    f"${{input:name}} references in {source.name} were rewritten "
                    f"to $name and 'argument-hint' was generated unless explicitly set."
                ),
            )

        # Defence-in-depth: scan compiled command before writing.  Uses
        # BLOCK_POLICY so a critical finding introduced by the transform
        # itself (e.g. via link resolution) prevents the file from being
        # written -- matches the secure-by-default contract of the pre-install
        # BLOCK gate that scans source files.
        # Fail-closed on missing/broken security gate (re-raise ImportError);
        # other I/O-style errors are surfaced as a warning so installs stay observable.
        compiled = frontmatter.dumps(post)
        scan_verdict = None
        try:
            scan_verdict = SecurityGate.scan_text(
                compiled,
                str(target),
                policy=BLOCK_POLICY,
            )
        except ImportError:
            # Missing/tampered gate must not silently become a no-op.
            raise
        except (OSError, ValueError) as exc:
            warnings.append(f"{target_name}: security scan skipped due to scan error: {exc}")

        security_messages = _collect_command_security_messages(scan_verdict, target)

        # Surface security findings via diagnostics.security() with correct severity.
        for message, detail, severity in security_messages:
            if diagnostics is not None:
                diagnostics.security(
                    message=message,
                    package=pkg_name,
                    detail=detail,
                    severity=severity,
                )
            else:
                logger.warning("%s: %s", message, detail)

        # Surface non-security warnings (e.g. parse / scan-error / rejected
        # input names) via the general warning channel so they do not get
        # miscategorised as security findings.
        _emit_command_warnings(warnings, diagnostics, logger, pkg_name)

        # Defence-in-depth skip: a critical post-transform finding must
        # not be deployed.  Surfaced as severity=critical above so the
        # user sees why nothing landed on disk.
        if scan_verdict is not None and scan_verdict.has_critical:
            return (links_resolved, False, bool(dropped_keys))

        # Ensure target directory exists.
        target.parent.mkdir(parents=True, exist_ok=True)

        # Write the command file.
        with open(target, "w", encoding="utf-8") as f:
            f.write(compiled)

        return (links_resolved, True, bool(dropped_keys))

    # ------------------------------------------------------------------
    # Target-driven API (data-driven dispatch)
    # ------------------------------------------------------------------

    def integrate_commands_for_target(
        self,
        target: TargetProfile,
        package_info,
        project_root: Path,
        opts: IntegrateOpts | None = None,
        **legacy_kwargs,
    ) -> IntegrationResult:
        """Integrate prompt files as commands for a single *target*.

        Reads deployment paths from *target*'s ``commands`` primitive
        mapping, applying the opt-in guard when ``auto_create`` is
        ``False``.
        """
        if opts is None and legacy_kwargs:
            opts = IntegrateOpts(
                force=legacy_kwargs.get("force", False),
                managed_files=legacy_kwargs.get("managed_files"),
                diagnostics=legacy_kwargs.get("diagnostics"),
            )
        resolved_opts = opts or IntegrateOpts()
        force = resolved_opts.force
        managed_files = resolved_opts.managed_files
        diagnostics = resolved_opts.diagnostics

        mapping = target.primitives.get("commands")
        if not mapping:
            return IntegrationResult(0, 0, 0, [], 0)

        # Hoist the per-package name lookup once -- used by every
        # diagnostic emitted below instead of being recomputed at each
        # call site (was duplicated 4x in this method).
        pkg_name = getattr(
            getattr(package_info, "package", None),
            "name",
            "",
        )

        effective_root = mapping.deploy_root or target.root_dir
        target_root = project_root / effective_root
        if not target.auto_create and not (project_root / target.root_dir).is_dir():
            # Surface a discoverability note so users (and CI logs) see
            # why the target was skipped.
            if diagnostics is not None:
                diagnostics.info(
                    message=(
                        f"Skipped {target.root_dir}/{mapping.subdir}/ -- "
                        f"create a {target.root_dir}/ directory to enable "
                        f"{target.name} command deployment."
                    ),
                    package=pkg_name,
                )
            return IntegrationResult(0, 0, 0, [], 0)

        prompt_files = self.find_prompt_files(package_info.install_path)
        if not prompt_files:
            return IntegrationResult(0, 0, 0, [], 0)

        # NOTE: the one-shot passthrough notice that used to fire here
        # is now emitted *after* the loop, gated on whether at least one
        # file in the batch actually had dropped frontmatter keys.  This
        # avoids polluting the happy path on Cursor installs of packages
        # whose prompts only use the cross-tool subset.
        self.init_link_resolver(package_info, project_root)

        commands_dir = target_root / mapping.subdir
        files_integrated = 0
        files_skipped = 0
        files_adopted = 0
        target_paths: list[Path] = []
        total_links_resolved = 0
        any_dropped_keys = False
        ctx = _CommandTargetContext(
            target=target,
            mapping=mapping,
            commands_dir=commands_dir,
            package_info=package_info,
            project_root=project_root,
            managed_files=managed_files,
            force=force,
            diagnostics=diagnostics,
            package_name=pkg_name,
        )

        for prompt_file in prompt_files:
            integrated, skipped, adopted, links_resolved, target_path, had_dropped = (
                _integrate_prompt_file(self, prompt_file, ctx)
            )
            files_integrated += integrated
            files_skipped += skipped
            files_adopted += adopted
            total_links_resolved += links_resolved
            any_dropped_keys = any_dropped_keys or had_dropped
            if target_path is not None:
                target_paths.append(target_path)

        # One-shot install-time notice for cursor-style targets that
        # actually dropped at least one frontmatter key in this batch.
        # Suppressed on the happy path (no dropped keys) to avoid
        # noise on Cursor installs of packages whose prompts only use
        # the cross-tool subset.  Per-file dropped-keys warnings already
        # fire from integrate_command() for keys that *are* discarded;
        # this one-shot info adds the cross-tool-compatibility context
        # so users who inspect ``.cursor/commands/*.md`` and see
        # Claude-style frontmatter understand it is intentional.
        if diagnostics is not None and self._should_emit_passthrough_notice(
            target.name,
            mapping.format_id,
            had_dropped_keys=any_dropped_keys,
        ):
            diagnostics.info(
                message=(
                    f"{target.name.capitalize()} command files keep "
                    f"Claude-compatible frontmatter (description, "
                    f"allowed-tools, model, argument-hint, input) "
                    f"intentionally for cross-tool compatibility."
                ),
                package=pkg_name,
            )

        return IntegrationResult(
            files_integrated=files_integrated,
            files_updated=0,
            files_skipped=files_skipped,
            target_paths=target_paths,
            links_resolved=total_links_resolved,
            files_adopted=files_adopted,
        )

    def sync_for_target(
        self,
        target: TargetProfile,
        apm_package,
        project_root: Path,
        managed_files: set | None = None,
    ) -> dict:
        """Remove APM-managed command files for a single *target*."""
        mapping = target.primitives.get("commands")
        if not mapping:
            return {"files_removed": 0, "errors": 0}
        effective_root = mapping.deploy_root or target.root_dir
        prefix = f"{effective_root}/{mapping.subdir}/"
        legacy_dir = project_root / effective_root / mapping.subdir
        return self.sync_remove_files(
            project_root,
            managed_files,
            prefix,
            SyncRemoveOpts(
                legacy_glob_dir=legacy_dir,
                legacy_glob_pattern="*-apm.md",
                targets=[target],
            ),
        )

    # ------------------------------------------------------------------
    # Gemini CLI Commands (.toml format)
    # ------------------------------------------------------------------

    @staticmethod
    def _write_gemini_command(source: Path, target: Path) -> None:
        """Transform ``.prompt.md`` to Gemini CLI ``.toml`` format (delegates to ``_transform``)."""
        _write_gemini_command_fn(source, target)
