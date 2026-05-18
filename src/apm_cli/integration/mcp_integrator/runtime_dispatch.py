"""Standalone MCP lifecycle orchestrator.

Owns all MCP dependency resolution, installation, stale cleanup, and lockfile
persistence logic.  This is NOT a BaseIntegrator subclass  -- MCP integration is
config-level orchestration (registry APIs, runtime configs, lockfile tracking),
not file-level deployment (copy/collision/sync).

The existing adapters (client/, package_manager/) and registry operations
(registry/operations.py) are *used* by this class, not modified.
"""

import builtins
import logging
import re
import shutil
from pathlib import Path

import click

from apm_cli.core.null_logger import NullCommandLogger
from apm_cli.integration.mcp_integrator_install.opts import RuntimeDispatchOpts
from apm_cli.utils import console as _console_utils
from apm_cli.utils.console import (
    _rich_error,
    _rich_info,
)

_log = logging.getLogger(__name__)


def _install_single_dependency(
    runtime: str, dep: str, *, opts: RuntimeDispatchOpts, logger
) -> bool:
    """Install one MCP dependency for one runtime."""
    from apm_cli.core.operations import install_package

    result = install_package(
        runtime,
        dep,
        shared_env_vars=opts.shared_env_vars,
        server_info_cache=opts.server_info_cache,
        shared_runtime_vars=opts.shared_runtime_vars,
        project_root=opts.project_root,
        user_scope=opts.user_scope,
    )
    if result["failed"]:
        logger.error(f"  Failed to install {dep}")
        return False
    if runtime != "codex":
        return True

    from apm_cli.factory import ClientFactory

    config_path = ClientFactory.create_client(
        runtime,
        project_root=opts.project_root,
        user_scope=opts.user_scope,
    ).get_config_path()
    _log.debug("Codex config written to %s", config_path)
    logger.verbose_detail(f"  Codex config: {config_path}")
    return True


def _echo_gate_message(message: str, *, level: str, symbol: str | None = None) -> None:
    """Emit gate diagnostics on stdout even if the shared console was redirected."""
    if level == "error":
        _rich_error(message, symbol=symbol)
    else:
        _rich_info(message, symbol=symbol)
    if getattr(_console_utils, "_console_stderr", False):
        status_symbols = getattr(_console_utils, "STATUS_SYMBOLS", {})
        prefix = f"{status_symbols[symbol]} " if symbol in status_symbols else ""
        click.echo(f"{prefix}{message}")


def _detect_runtimes(scripts: dict) -> list[str]:
    """Extract runtime commands from apm.yml scripts."""
    # CRITICAL: Use builtins.set explicitly to avoid Click command collision!
    detected = builtins.set()

    for script_name, command in scripts.items():  # noqa: B007
        if re.search(r"\bcopilot\b", command):
            detected.add("copilot")
        if re.search(r"\bcodex\b", command):
            detected.add("codex")
        if re.search(r"\bgemini\b", command):
            detected.add("gemini")
        if re.search(r"\bclaude\b", command):
            detected.add("claude")
        if re.search(r"\bllm\b", command):
            detected.add("llm")
        if re.search(r"\bwindsurf\b", command):
            detected.add("windsurf")

    return builtins.list(detected)


def _filter_runtimes(detected_runtimes: list[str]) -> list[str]:
    """Filter to only runtimes that are actually installed and support MCP."""
    from apm_cli.factory import ClientFactory

    # First filter to only MCP-compatible runtimes
    try:
        mcp_compatible = []
        for rt in detected_runtimes:
            try:
                ClientFactory.create_client(rt)
                mcp_compatible.append(rt)
            except ValueError:
                continue

        # Then filter to only installed runtimes
        try:
            from apm_cli.runtime.manager import RuntimeManager

            manager = RuntimeManager()
            return [rt for rt in mcp_compatible if manager.is_runtime_available(rt)]
        except ImportError:
            available = []
            for rt in mcp_compatible:
                if shutil.which(rt):
                    available.append(rt)
            return available

    except ImportError:
        # Derived from ClientFactory; see _MCP_CLIENT_REGISTRY.
        from apm_cli.factory import ClientFactory

        mcp_compatible = [rt for rt in detected_runtimes if rt in ClientFactory.supported_clients()]
        return [rt for rt in mcp_compatible if shutil.which(rt)]


def _install_for_runtime(
    runtime: str,
    mcp_deps: list[str],
    opts: RuntimeDispatchOpts | None = None,
) -> bool:
    """Install MCP dependencies for a specific runtime.

    Returns True if all deps were configured successfully, False otherwise.
    """
    resolved_opts = opts or RuntimeDispatchOpts()
    logger = resolved_opts.logger
    if logger is None:
        logger = NullCommandLogger()
    try:
        all_ok = True
        for dep in mcp_deps:
            logger.verbose_detail(f"  Installing {dep}...")
            try:
                all_ok = (
                    _install_single_dependency(
                        runtime,
                        dep,
                        opts=resolved_opts,
                        logger=logger,
                    )
                    and all_ok
                )
            except Exception as install_error:
                _log.debug(
                    "Failed to install MCP dep %s for runtime %s",
                    dep,
                    runtime,
                    exc_info=True,
                )
                logger.error(f"  Failed to install {dep}: {install_error}")
                all_ok = False

        # Emit aggregated post-install diagnostics for runtimes that
        # support runtime env-var substitution (currently Copilot CLI).
        # Safe no-op for runtimes whose adapter doesn't aggregate state.
        try:
            if runtime == "copilot":
                from apm_cli.adapters.client.copilot import CopilotClientAdapter

                CopilotClientAdapter.emit_install_run_summary()
        except Exception:
            _log.debug("Failed to emit install-run summary", exc_info=True)

        return all_ok

    except ImportError as e:
        logger.warning(f"Core operations not available for runtime {runtime}: {e}")
        logger.progress(f"Dependencies for {runtime}: {', '.join(mcp_deps)}")
        return False
    except ValueError as e:
        logger.warning(f"Runtime {runtime} not supported: {e}")
        logger.progress(
            "Supported runtimes: vscode, copilot, codex, cursor, opencode, gemini, claude, windsurf, llm"
        )
        return False
    except Exception as e:
        _log.debug("Unexpected error installing for runtime %s", runtime, exc_info=True)
        logger.error(f"Error installing for runtime {runtime}: {e}")
        return False


def _gate_project_scoped_runtimes(
    target_runtimes: list[str],
    *,
    user_scope: bool,
    project_root,
    apm_config: dict | None,
    explicit_target: str | list[str] | None,
) -> list[str]:
    """Filter *target_runtimes* against the project's active targets.

    UX parity with ``apm install`` for apm dependencies: the active
    target set (explicit ``--target`` > ``targets:`` field >
    directory-signal detection) is the whitelist for MCP writes. Any
    runtime outside that set is skipped with an info line naming both
    what was dropped and the active set, so users can audit the
    decision input without re-reading apm.yml (#1335).

    Strict resolution model -- mirrors :func:`resolve_targets`,
    the same call ``apm install`` uses
    (``install/phases/targets.py:233``):

      - flag > yaml-targets > directory signals (no permissive
        "fallback to copilot" greenfield default);
      - no flag, no ``targets:``, and no harness-signal directory ->
        :class:`NoHarnessError` (red ``[x]``, write nothing);
      - multiple ambiguous signals with no disambiguation ->
        :class:`AmbiguousHarnessError` (same fail-closed shape).

    ``explicit_target`` accepts ``str``, ``list[str]``, or a CSV
    string (``"claude,copilot"``) -- the latter is produced by
    legacy callers; it is normalized to a list before the resolver
    is invoked so the canonical-name validator does not reject it as
    one unknown token.

    A malformed ``targets:`` field (conflicting ``target:`` +
    ``targets:``, ``targets: []``, or unknown canonical name) likewise
    fails closed: nothing is written.

    Exit semantics differ deliberately from ``install/phases/targets.py``:
    the canonical install phase calls ``raise SystemExit(2)`` when
    resolution fails; this gate may be invoked mid-bundle (see
    ``install/local_bundle_handler``) where a hard exit would corrupt
    partial state, so we render the same red ``[x]`` voice and return
    an empty list (fail-closed-continue).

    ``user_scope=True`` is a deliberate carve-out: user-scope writes
    target ``~/.config`` paths the user owns globally, so the
    project-level whitelist is irrelevant. Documented in the
    consumer install-mcp-servers guide.
    """
    if user_scope:
        return target_runtimes

    from apm_cli.core.apm_yml import (
        ConflictingTargetsError,
        EmptyTargetsListError,
        UnknownTargetError,
        parse_targets_field,
    )
    from apm_cli.core.errors import (
        AmbiguousHarnessError,
        NoHarnessError,
    )
    from apm_cli.core.target_detection import resolve_targets
    from apm_cli.integration.targets import RUNTIME_TO_CANONICAL_TARGET

    # --- step 1: parse declared targets (fail-closed on any invalid form)
    yaml_targets: list[str] | None = None
    if apm_config:
        try:
            parsed = parse_targets_field(apm_config)
            yaml_targets = parsed if parsed else None
        except (
            ConflictingTargetsError,
            EmptyTargetsListError,
            UnknownTargetError,
        ) as exc:
            # Voice mirrors the canonical `apm install` skills phase
            # (install/phases/targets.py:213): red [x] lead-with-outcome,
            # then the structured error body. symbol="" suppresses the
            # auto-prefix on the body because the exception text already
            # begins with "[x] ..." (see core/errors.py).
            _echo_gate_message(
                "Skipping all MCP config writes -- apm.yml 'targets' field is invalid.",
                level="error",
                symbol="error",
            )
            _echo_gate_message(str(exc), level="error", symbol="")
            _log.debug(
                "parse_targets_field failed; failing closed (no MCP writes)",
                exc_info=True,
            )
            return []

    # --- step 2: normalize CSV explicit_target sugar to a list -----
    # `_wire_bundle_mcp_servers` historically passes a CSV string; the
    # canonical-name validator inside _resolve_targets_v2 would reject
    # the whole CSV as one unknown token. Normalize first.
    flag: str | list[str] | None
    if isinstance(explicit_target, str) and "," in explicit_target:
        flag = [t.strip() for t in explicit_target.split(",") if t.strip()]
    else:
        flag = explicit_target

    # Apply the runtime->canonical-target alias BEFORE passing the flag
    # to resolve_targets. The canonical-name validator inside the
    # resolver only knows about CANONICAL_TARGETS (claude/copilot/...);
    # it rejects runtime aliases (vscode/agents) as unknown tokens.
    # The MCP gate, however, must accept those aliases because users
    # naturally type `--target vscode` for the VS Code Copilot runtime.
    if flag is not None:
        tokens = [flag] if isinstance(flag, str) else list(flag)
        flag = [RUNTIME_TO_CANONICAL_TARGET.get(t, t) for t in tokens]

    if flag is None and apm_config is None and project_root is None:
        return target_runtimes

    # --- step 3: delegate to the canonical v2 resolver -------------
    # When the caller supplied explicit target intent (flag or apm.yml),
    # mirror the canonical install target resolver. Otherwise keep the
    # already-detected runtime set unchanged.
    root = project_root or Path.cwd()
    try:
        resolved = resolve_targets(root, flag=flag, yaml_targets=yaml_targets)
    except (NoHarnessError, AmbiguousHarnessError) as exc:
        _echo_gate_message(
            "Skipping all MCP config writes -- could not resolve active targets.",
            level="error",
            symbol="error",
        )
        _echo_gate_message(str(exc), level="error", symbol="")
        _log.debug(
            "resolve_targets failed; failing closed (no MCP writes)",
            exc_info=True,
        )
        return []

    active = set(resolved.targets)

    # Runtime name "vscode" maps to canonical target "copilot" (same
    # alias active_targets honors); shared table prevents drift with
    # the alias resolution in integration/targets.py.
    out = [rt for rt in target_runtimes if RUNTIME_TO_CANONICAL_TARGET.get(rt, rt) in active]
    dropped = sorted(set(target_runtimes) - set(out))
    if dropped:
        # Mirror the canonical `Targets: X  (source: Y)` provenance shape
        # (install/phases/targets.py:265, core/target_detection.py:777):
        # double-space before the parenthetical. The "or '<none>'" guard is
        # defensive -- an empty active set is unreachable when
        # _resolve_targets_v2 succeeded, but if a future contract change
        # widens that contract we surface "<none>" rather than render
        # "(active targets: )" which reads as a renderer bug.
        active_csv = ", ".join(sorted(active)) or "<none>"
        _echo_gate_message(
            f"Skipped MCP config for {', '.join(dropped)}  (active targets: {active_csv})",
            level="info",
            symbol="info",
        )
        _log.debug(
            "Active-targets gate dropped: %s (active=%s)",
            dropped,
            sorted(active),
        )
    return out
