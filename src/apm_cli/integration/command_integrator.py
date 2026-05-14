"""Command integration functionality for APM packages.

Integrates .prompt.md files as commands for any target that supports the
``commands`` primitive (e.g. ``.claude/commands/``, ``.opencode/commands/``).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple  # noqa: F401, UP035

import frontmatter

from apm_cli.integration.base_integrator import BaseIntegrator, IntegrationResult
from apm_cli.security.gate import BLOCK_POLICY, SecurityGate
from apm_cli.utils.path_security import (
    PathTraversalError,
    ensure_path_within,
    validate_path_segments,
)
from apm_cli.utils.paths import portable_relpath

if TYPE_CHECKING:
    from apm_cli.integration.targets import TargetProfile
    from apm_cli.utils.diagnostics import DiagnosticCollector

logger = logging.getLogger(__name__)


# Allowlist for argument names extracted from package-supplied 'input:' front-matter.
# Restricts to identifiers that are safe to embed in YAML frontmatter and in
# Claude command bodies as $name placeholders. Rejects YAML-significant
# characters (newline, colon, quote, etc.) to prevent frontmatter injection.
_INPUT_NAME_RE = re.compile(r"^[A-Za-z][\w-]{0,63}$")


# Frontmatter keys preserved (or consumed) by the shared claude_command
# transformer.  Any key in source frontmatter not in this set is dropped
# during transformation and surfaced as a diagnostic warning so package
# authors can act on it.  See command_integrator.integrate_command().
_PRESERVED_COMMAND_KEYS = frozenset(
    {
        "description",
        "allowed-tools",
        "allowedTools",
        "model",
        "argument-hint",
        "argumentHint",
        "input",
    }
)

# User-facing display names for preserved keys.  Excludes camelCase
# aliases (allowedTools, argumentHint) -- those are accepted on input
# for compat but the canonical kebab-case form is what we surface to
# package authors in diagnostic messages.
_PRESERVED_COMMAND_KEYS_DISPLAY = frozenset(
    {
        "description",
        "allowed-tools",
        "model",
        "argument-hint",
        "input",
    }
)


def _is_valid_input_name(name: str) -> bool:
    """Return True if *name* is a safe argument identifier."""
    return bool(_INPUT_NAME_RE.match(name))


def _extract_input_names(
    input_spec: Any,
) -> tuple[list[str], list[str]]:
    """Extract argument names from an APM 'input' front-matter value.

    Handles both formats:
      - Simple list:  input: [name, category]
      - Object list:  input:
                        - feature_name: "desc"
                        - feature_description: "desc"

    Args:
        input_spec: The raw value of the 'input' front-matter key.

    Returns:
        Tuple[List[str], List[str]]: (valid names in order, rejected raw entries).
        Names are accepted only if they match ``^[A-Za-z][\\w-]{0,63}$``;
        anything else (empty/whitespace, YAML-significant chars, oversize) is
        rejected and reported back so the caller can surface a warning.
    """
    valid: list[str] = []
    rejected: list[str] = []

    def _accept(candidate: Any) -> None:
        if not isinstance(candidate, str):
            rejected.append(repr(candidate))
            return
        stripped = candidate.strip()
        if not stripped:
            return  # silently drop pure-whitespace entries
        if _is_valid_input_name(stripped):
            valid.append(stripped)
        else:
            rejected.append(stripped)

    if input_spec is None:
        return valid, rejected

    if isinstance(input_spec, list):
        for item in input_spec:
            if isinstance(item, str):
                _accept(item)
            elif isinstance(item, dict):
                for k in item.keys():  # noqa: SIM118
                    _accept(k)
            else:
                rejected.append(repr(item))
        return valid, rejected

    if isinstance(input_spec, str):
        _accept(input_spec)
        return valid, rejected

    if isinstance(input_spec, dict):
        for k in input_spec.keys():  # noqa: SIM118
            _accept(k)
        return valid, rejected

    return valid, rejected


# Re-export for backward compat (tests import CommandIntegrationResult)
CommandIntegrationResult = IntegrationResult


class CommandIntegrator(BaseIntegrator):
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
        """Return True the first time *target_name* sees a passthrough deploy
        in which at least one file actually had dropped keys.

        Only fires for cursor-style targets that reuse the shared
        ``claude_command`` transformer (and would benefit from the
        cross-tool-compatibility explanation).  Returns False for
        targets that have their own dedicated writer (e.g. Gemini),
        and -- per review feedback -- returns False on the happy path
        where no frontmatter keys were dropped (the notice would be
        pure noise then).
        """
        if not had_dropped_keys:
            return False
        if format_id != "claude_command" or target_name == "claude":
            return False
        if target_name in self._passthrough_notified:
            return False
        self._passthrough_notified.add(target_name)
        return True

    def find_prompt_files(self, package_path: Path) -> list[Path]:
        """Find all .prompt.md files in a package."""
        return self.find_files_by_glob(package_path, "*.prompt.md", subdirs=[".apm/prompts"])

    def _transform_prompt_to_command(
        self,
        source: Path,
    ) -> tuple[str, frontmatter.Post, list[str], list[str]]:
        """Transform a .prompt.md file into Claude command format.

        Args:
            source: Path to the .prompt.md file

        Returns:
            Tuple of (command_name, post, warnings, dropped_keys).
            ``dropped_keys`` lists source frontmatter keys that the shared
            command transformer does not preserve (e.g. ``author``,
            ``mcp``, ``parameters`` for Cursor-specific frontmatter).
        """
        warnings: list[str] = []

        post = frontmatter.load(source)

        # Extract command name from filename
        filename = source.name
        if filename.endswith(".prompt.md"):
            command_name = filename[: -len(".prompt.md")]
        else:
            command_name = source.stem

        # Build Claude command frontmatter (preserve existing, add Claude-specific)
        claude_metadata = {}

        # Map APM frontmatter to Claude frontmatter
        if "description" in post.metadata:
            claude_metadata["description"] = post.metadata["description"]

        if "allowed-tools" in post.metadata:
            claude_metadata["allowed-tools"] = post.metadata["allowed-tools"]
        elif "allowedTools" in post.metadata:
            claude_metadata["allowed-tools"] = post.metadata["allowedTools"]

        if "model" in post.metadata:
            claude_metadata["model"] = post.metadata["model"]

        if "argument-hint" in post.metadata:
            claude_metadata["argument-hint"] = post.metadata["argument-hint"]
        elif "argumentHint" in post.metadata:
            claude_metadata["argument-hint"] = post.metadata["argumentHint"]

        # Map APM 'input' to Claude 'arguments' and 'argument-hint'
        input_names, rejected_names = _extract_input_names(post.metadata.get("input"))
        if rejected_names:
            warnings.append(
                f"input: rejected {len(rejected_names)} invalid name(s) "
                f"(must match [A-Za-z][\\w-]{{0,63}}): "
                f"{', '.join(rejected_names[:5])}" + (" ..." if len(rejected_names) > 5 else "")
            )
        if input_names:
            claude_metadata["arguments"] = input_names
            if "argument-hint" not in claude_metadata:
                claude_metadata["argument-hint"] = " ".join(f"<{name}>" for name in input_names)

        # Convert APM input references to Claude $name placeholders
        content = post.content
        if input_names:
            content = re.sub(
                r"\$\{\{?\s*input\s*:\s*([\w-]+)\s*\}?\}",
                r"$\1",
                content,
            )

        # Create new post with Claude metadata
        new_post = frontmatter.Post(content)
        new_post.metadata = claude_metadata

        # Compute keys present in source frontmatter but not preserved by
        # the shared command transformer.  Surfaced by integrate_command()
        # via diagnostics so users see the lossy transform at install time.
        dropped_keys = sorted(set(post.metadata.keys()) - _PRESERVED_COMMAND_KEYS)

        return (command_name, new_post, warnings, dropped_keys)

    def integrate_command(
        self,
        source: Path,
        target: Path,
        package_info: Any,
        original_path: Path,
        *,
        diagnostics: DiagnosticCollector | None = None,
        target_name: str = "claude",
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
            source: Source .prompt.md file path
            target: Target command file path (e.g. .claude/commands/foo.md
                    or .cursor/commands/foo.md)
            package_info: PackageInfo object with package metadata
            original_path: Original path to the prompt file
            diagnostics: Optional DiagnosticCollector for surfacing warnings.
            target_name: Name of the deployment target (e.g. ``"claude"``,
                ``"cursor"``, ``"opencode"``) so diagnostic messages stay
                target-agnostic instead of always saying "Claude".

        Returns:
            tuple[int, bool, bool]: (links_resolved, written, had_dropped_keys).
            ``written`` is False when a critical post-transform security
            finding causes the write to be skipped (defense-in-depth on
            top of the pre-install BLOCK gate).  ``had_dropped_keys`` is
            True when the source frontmatter carried at least one key
            outside the cross-tool subset preserved by the shared
            ``claude_command`` transformer; the dispatcher uses this to
            decide whether to surface the one-shot passthrough notice.
        """
        # Transform to command format
        command_name, post, warnings, dropped_keys = self._transform_prompt_to_command(source)

        # Resolve context links in content
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
        # users aren't surprised by content that differs from the source package.
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

        # Defense-in-depth: scan compiled command before writing.  Uses
        # BLOCK_POLICY so a critical finding introduced by the
        # transform itself (e.g. via link resolution) prevents the file
        # from being written -- matches the secure-by-default contract
        # of the pre-install BLOCK gate that scans source files.
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
            warnings.append(f"{target.name}: security scan skipped due to scan error: {exc}")

        security_messages: list[tuple[str, str, str]] = []
        if scan_verdict is not None:
            if scan_verdict.has_critical:
                security_messages.append(
                    (
                        f"Critical hidden characters in {target.name}",
                        (
                            f"{scan_verdict.critical_count} critical, "
                            f"{scan_verdict.warning_count} warning(s) -- "
                            f"run 'apm audit --file {target}' to inspect"
                        ),
                        "critical",
                    )
                )
            elif scan_verdict.has_findings:
                security_messages.append(
                    (
                        f"Hidden character warnings in {target.name}",
                        (
                            f"{scan_verdict.warning_count} warning(s) -- "
                            f"run 'apm audit --file {target}' to inspect"
                        ),
                        "warning",
                    )
                )

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
        # input names) via the general warning channel so they don't get
        # miscategorized as security findings.
        for warning in warnings:
            if diagnostics is not None:
                diagnostics.warn(
                    message=warning,
                    package=pkg_name,
                )
            else:
                logger.warning(warning)

        # Defense-in-depth skip: a critical post-transform finding must
        # not be deployed.  Surfaced as severity=critical above so the
        # user sees why nothing landed on disk.
        if scan_verdict is not None and scan_verdict.has_critical:
            return (links_resolved, False, bool(dropped_keys))

        # Ensure target directory exists
        target.parent.mkdir(parents=True, exist_ok=True)

        # Write the command file
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
        *,
        force: bool = False,
        managed_files: set = None,  # noqa: RUF013
        diagnostics=None,
    ) -> IntegrationResult:
        """Integrate prompt files as commands for a single *target*.

        Reads deployment paths from *target*'s ``commands`` primitive
        mapping, applying the opt-in guard when ``auto_create`` is
        ``False``.
        """
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

        for prompt_file in prompt_files:
            filename = prompt_file.name
            if filename.endswith(".prompt.md"):
                base_name = filename[: -len(".prompt.md")]
            else:
                base_name = prompt_file.stem

            # Containment: reject base names containing path traversal
            # sequences before joining into commands_dir.  A malicious
            # package shipping ``../../evil.prompt.md`` would otherwise
            # escape the target commands directory.
            try:
                validate_path_segments(base_name, context="command filename")
            except PathTraversalError as exc:
                if diagnostics is not None:
                    diagnostics.warn(
                        message=f"Rejected command filename: {exc}",
                        package=pkg_name,
                    )
                files_skipped += 1
                continue

            target_path = commands_dir / f"{base_name}{mapping.extension}"

            # Defense-in-depth: assert the resolved target stays inside
            # the commands directory even if validate_path_segments was
            # bypassed by a future regression.
            try:
                ensure_path_within(target_path, commands_dir)
            except PathTraversalError as exc:
                if diagnostics is not None:
                    diagnostics.warn(
                        message=f"Rejected command target path: {exc}",
                        package=pkg_name,
                    )
                files_skipped += 1
                continue

            rel_path = portable_relpath(target_path, project_root)

            if self.is_content_identical_to_source(target_path, prompt_file):
                # Pre-existing file is byte-identical to source -- silently
                # adopt. See BaseIntegrator.is_content_identical_to_source.
                target_paths.append(target_path)
                files_adopted += 1
                continue

            if self.check_collision(
                target_path,
                rel_path,
                managed_files,
                force,
                diagnostics=diagnostics,
            ):
                files_skipped += 1
                continue

            if mapping.format_id == "gemini_command":
                self._write_gemini_command(prompt_file, target_path)
                links_resolved = 0
                written = True
                had_dropped = False
            else:
                # Cursor reuses the shared claude_command transformer;
                # pass target.name so diagnostic messages stay
                # target-agnostic (no Claude branding for Cursor
                # installs).  See the cursor-command-format TODO on
                # KNOWN_TARGETS["cursor"]["commands"] in targets.py.
                links_resolved, written, had_dropped = self.integrate_command(
                    prompt_file,
                    target_path,
                    package_info,
                    prompt_file,
                    diagnostics=diagnostics,
                    target_name=target.name,
                )
            if not written:
                # Critical post-transform finding -- defense-in-depth
                # skip already surfaced via diagnostics.security().
                files_skipped += 1
                continue
            if had_dropped:
                any_dropped_keys = True
            files_integrated += 1
            total_links_resolved += links_resolved
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
        managed_files: set = None,  # noqa: RUF013
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
            prefix=prefix,
            legacy_glob_dir=legacy_dir,
            legacy_glob_pattern="*-apm.md",
            targets=[target],
        )

    # ------------------------------------------------------------------
    # Gemini CLI Commands (.toml format)
    # ------------------------------------------------------------------

    @staticmethod
    def _write_gemini_command(source: Path, target: Path) -> None:
        """Transform a ``.prompt.md`` file to Gemini CLI ``.toml`` format.

        Parses YAML frontmatter for ``description``, uses the markdown
        body as the ``prompt`` field.  Replaces ``$ARGUMENTS`` with
        ``{{args}}`` (Gemini CLI's argument interpolation syntax).

        Ref: https://geminicli.com/docs/cli/gemini-md/
        """
        import toml as _toml

        post = frontmatter.load(source)

        description = post.metadata.get("description", "")
        prompt_text = post.content.strip()
        prompt_text = prompt_text.replace("$ARGUMENTS", "{{args}}")

        if re.search(r"(?<!\d)\$\d+", prompt_text):
            prompt_text = f"Arguments: {{{{args}}}}\n\n{prompt_text}"

        doc = {"prompt": prompt_text}
        if description:
            doc = {"description": description, "prompt": prompt_text}

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_toml.dumps(doc), encoding="utf-8")

    # ------------------------------------------------------------------
    # Legacy per-target API (DEPRECATED)
    #
    # These methods hardcode a specific target and bypass scope
    # resolution.  Use the target-driven API (*_for_target) with
    # profiles from resolve_targets() instead.
    #
    # Kept for backward compatibility with external consumers.
    # Do NOT add new per-target methods here.
    # ------------------------------------------------------------------

    # DEPRECATED: use integrate_commands_for_target(KNOWN_TARGETS["claude"], ...) instead.
    def integrate_package_commands(
        self,
        package_info,
        project_root: Path,
        force: bool = False,
        managed_files: set = None,  # noqa: RUF013
        diagnostics=None,
    ) -> IntegrationResult:
        """Integrate prompt files as Claude commands (.claude/commands/).

        Legacy compat: ensures ``.claude/`` exists so the target-driven
        method does not skip.
        """
        from apm_cli.integration.targets import KNOWN_TARGETS

        (project_root / ".claude").mkdir(parents=True, exist_ok=True)
        return self.integrate_commands_for_target(
            KNOWN_TARGETS["claude"],
            package_info,
            project_root,
            force=force,
            managed_files=managed_files,
            diagnostics=diagnostics,
        )

    # DEPRECATED: use sync_for_target(KNOWN_TARGETS["claude"], ...) instead.
    def sync_integration(self, apm_package, project_root: Path, managed_files: set = None) -> dict:  # noqa: RUF013
        """Remove APM-managed command files from .claude/commands/."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        return self.sync_for_target(
            KNOWN_TARGETS["claude"],
            apm_package,
            project_root,
            managed_files=managed_files,
        )

    # DEPRECATED: use sync_for_target(KNOWN_TARGETS["claude"], ...) instead.
    def remove_package_commands(
        self,
        package_name: str,
        project_root: Path,
        managed_files: set = None,  # noqa: RUF013
    ) -> int:
        """Remove APM-managed command files."""
        stats = self.sync_integration(None, project_root, managed_files=managed_files)
        return stats["files_removed"]

    # DEPRECATED: use integrate_commands_for_target(KNOWN_TARGETS["opencode"], ...) instead.
    def integrate_package_commands_opencode(
        self,
        package_info,
        project_root: Path,
        force: bool = False,
        managed_files: set = None,  # noqa: RUF013
        diagnostics=None,
    ) -> IntegrationResult:
        """Integrate prompt files as OpenCode commands (.opencode/commands/)."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        return self.integrate_commands_for_target(
            KNOWN_TARGETS["opencode"],
            package_info,
            project_root,
            force=force,
            managed_files=managed_files,
            diagnostics=diagnostics,
        )

    # DEPRECATED: use sync_for_target(KNOWN_TARGETS["opencode"], ...) instead.
    def sync_integration_opencode(
        self,
        apm_package,
        project_root: Path,
        managed_files: set = None,  # noqa: RUF013
    ) -> dict:
        """Remove APM-managed command files from .opencode/commands/."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        return self.sync_for_target(
            KNOWN_TARGETS["opencode"],
            apm_package,
            project_root,
            managed_files=managed_files,
        )
