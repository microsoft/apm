"""``apm hooks`` -- inspect, test, and scaffold lifecycle hooks.

Sub-commands:

* ``apm hooks``          -- list discovered hooks
* ``apm hooks test``     -- dry-run a synthetic event through all hooks
* ``apm hooks init``     -- scaffold a starter hook JSON file
* ``apm hooks validate`` -- check all hook files for errors
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import urlparse

import click

from apm_cli.utils.console import (
    STATUS_SYMBOLS,
    _rich_echo,
    _rich_error,
    _rich_info,
    _rich_success,
    _rich_warning,
)

# Default scaffold template.
_INIT_TEMPLATE = {
    "version": 1,
    "hooks": {
        "post-install": [
            {
                "type": "command",
                "bash": "echo 'Installed:' && cat",
                "timeoutSec": 10,
            }
        ],
    },
}


@click.group(
    invoke_without_command=True,
    help="Inspect, test, and scaffold lifecycle hooks.",
)
@click.pass_context
def hooks(ctx: click.Context) -> None:
    """List discovered lifecycle hooks when invoked without a sub-command."""
    if ctx.invoked_subcommand is not None:
        return

    from apm_cli.core.lifecycle_hooks import discover_hooks

    project_root = str(Path.cwd())
    entries = discover_hooks(project_root=project_root)

    if not entries:
        _rich_info("No lifecycle hooks discovered.", symbol="info")
        _rich_echo(
            "  Create one with: apm hooks init",
            style="dim",
        )
        return

    _rich_echo(
        f"{STATUS_SYMBOLS['check']} Discovered {len(entries)} hook(s):\n",
        style="green",
    )

    try:
        from rich.table import Table

        from apm_cli.utils.console import _get_console

        console = _get_console()
        if console is None:
            raise ImportError("Rich console unavailable")

        table = Table(
            title="Lifecycle Hooks",
            show_header=True,
            header_style="bold cyan",
        )
        table.add_column("Event", style="bold white")
        table.add_column("Type", style="cyan")
        table.add_column("Target", style="white")
        table.add_column("Source", style="dim")

        for entry in entries:
            target = entry.url or entry.effective_command or "(none)"
            table.add_row(entry.event, entry.hook_type, target, entry.source)

        console.print(table)
    except (ImportError, NameError):
        for entry in entries:
            target = entry.url or entry.effective_command or "(none)"
            click.echo(f"  {entry.event:20s} {entry.hook_type:10s} {target} ({entry.source})")


@hooks.command(
    name="test",
    help="Dry-run a synthetic lifecycle event through discovered hooks.",
)
@click.argument(
    "event",
    required=False,
    default="post-install",
    type=click.Choice(
        [
            "pre-install",
            "post-install",
            "pre-update",
            "post-update",
            "pre-uninstall",
            "post-uninstall",
        ],
        case_sensitive=False,
    ),
)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def hooks_test(event: str, verbose: bool) -> None:
    """Fire a synthetic event through all discovered hooks."""
    from apm_cli.core.lifecycle_hooks import (
        LifecycleEvent,
        PackageInfo,
        build_runner_from_context,
    )

    project_root = str(Path.cwd())
    runner = build_runner_from_context(project_root=project_root, verbose=verbose)

    matching = runner.hooks_for_event(event)
    if not matching:
        _rich_warning(
            f"No hooks registered for '{event}'. Create one with: apm hooks init",
            symbol="warning",
        )
        return

    _rich_info(
        f"Firing synthetic '{event}' event ({len(matching)} hook(s))...",
        symbol="gear",
    )

    synthetic_event = LifecycleEvent.create(
        event=event,
        packages=[PackageInfo(name="test/synthetic-package", reference="v0.0.0-test")],
        scope="project",
        working_directory=project_root,
    )

    threads = runner.fire(event, synthetic_event)

    # Drain HTTP daemon threads so log entries are written before exit.
    for t in threads:
        t.join(timeout=15)

    _rich_success(
        f"'{event}' event fired. Check ~/.apm/logs/hooks.log for output.",
        symbol="check",
    )


@hooks.command(
    name="init",
    help="Scaffold a starter hook JSON file at .apm/hooks.json.",
)
@click.option("--force", is_flag=True, help="Overwrite if file already exists.")
def hooks_init(force: bool) -> None:
    """Create a starter hook JSON file in the project."""
    apm_dir = Path.cwd() / ".apm"
    target_file = apm_dir / "hooks.json"

    if target_file.exists() and not force:
        _rich_warning(
            f"Hook file already exists: {target_file.relative_to(Path.cwd())}",
            symbol="warning",
        )
        _rich_echo("  Use --force to overwrite.", style="dim")
        return

    apm_dir.mkdir(parents=True, exist_ok=True)

    content = json.dumps(_INIT_TEMPLATE, indent=2) + "\n"
    target_file.write_text(content, encoding="utf-8")

    _rich_success(
        f"Created hook file: {target_file.relative_to(Path.cwd())}",
        symbol="check",
    )
    _rich_echo("")

    try:
        from rich.panel import Panel

        from apm_cli.utils.console import _get_console

        console = _get_console()
        if console is None:
            raise ImportError("Rich console unavailable")

        console.print(
            Panel(
                "[bold]Next steps:[/bold]\n\n"
                f"  1. Edit [cyan]{target_file.relative_to(Path.cwd())}[/cyan] "
                "to add your hooks\n"
                "  2. Run [cyan]apm hooks validate[/cyan] to check for errors\n"
                "  3. Run [cyan]apm hooks test post-install[/cyan] to dry-run\n",
                title="Getting Started",
                style="cyan",
            )
        )
    except (ImportError, NameError):
        click.echo("Next steps:")
        click.echo(f"  1. Edit {target_file.relative_to(Path.cwd())} to add your hooks")
        click.echo("  2. Run `apm hooks validate` to check for errors")
        click.echo("  3. Run `apm hooks test post-install` to dry-run")


@hooks.command(
    name="validate",
    help="Validate all discovered hook files for errors.",
)
def hooks_validate() -> None:
    """Check all hook JSON files across discovery sources for errors."""
    from apm_cli.core.lifecycle_hooks import (
        _get_policy_hooks_dir,
        _get_project_hooks_file,
        _get_user_hooks_dir,
    )

    project_root = str(Path.cwd())
    dirs = [
        ("policy", _get_policy_hooks_dir()),
        ("user", _get_user_hooks_dir()),
    ]

    total_files = 0
    total_errors = 0
    total_hooks = 0

    def _process_file(json_file: Path, source: str) -> None:
        nonlocal total_files, total_errors, total_hooks
        total_files += 1
        errors = _validate_hook_file(json_file, source)
        if errors:
            total_errors += len(errors)
            rel = json_file.relative_to(Path.cwd()) if source == "project" else json_file
            _rich_error(f"{rel}:", symbol="error")
            for err in errors:
                _rich_echo(f"    {err}", style="red")
        else:
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
                hook_count = sum(
                    len(v) for v in data.get("hooks", {}).values() if isinstance(v, list)
                )
                total_hooks += hook_count
            except Exception:
                pass

    # Check directory-based sources (policy, user).
    for source, directory in dirs:
        if not directory.is_dir():
            continue
        for json_file in sorted(directory.glob("*.json")):
            if json_file.is_file():
                _process_file(json_file, source)

    # Check project-level single file.
    project_file = _get_project_hooks_file(project_root)
    if project_file.is_file():
        _process_file(project_file, "project")

    if total_files == 0:
        _rich_info("No hook files found.", symbol="info")
        _rich_echo("  Create one with: apm hooks init", style="dim")
        return

    if total_errors > 0:
        _rich_error(
            f"{total_errors} error(s) in {total_files} file(s).",
            symbol="error",
        )
        sys.exit(1)
    else:
        _rich_success(
            f"All {total_files} hook file(s) valid ({total_hooks} hook(s) configured).",
            symbol="check",
        )


def _validate_hook_file(path: Path, source: str) -> list[str]:
    """Validate a single hook JSON file. Returns a list of error messages."""
    from apm_cli.core.lifecycle_hooks import (
        HOOK_FILE_VERSION,
        HOOK_TYPES,
        LIFECYCLE_EVENTS,
    )

    errors: list[str] = []

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        return [f"Cannot read file: {e}"]

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return [f"Invalid JSON: {e}"]

    if not isinstance(data, dict):
        return ["Root must be a JSON object"]

    version = data.get("version")
    if version is None:
        errors.append("Missing 'version' field")
    elif version != HOOK_FILE_VERSION:
        errors.append(f"Unsupported version: {version} (expected {HOOK_FILE_VERSION})")

    hooks_dict = data.get("hooks")
    if hooks_dict is None:
        errors.append("Missing 'hooks' field")
        return errors

    if not isinstance(hooks_dict, dict):
        errors.append("'hooks' must be a JSON object")
        return errors

    for event_name, hook_list in hooks_dict.items():
        if event_name not in LIFECYCLE_EVENTS:
            errors.append(f"Unknown event: '{event_name}'")
            continue

        if not isinstance(hook_list, list):
            errors.append(f"'{event_name}' must be an array")
            continue

        for i, entry in enumerate(hook_list):
            prefix = f"{event_name}[{i}]"

            if not isinstance(entry, dict):
                errors.append(f"{prefix}: must be a JSON object")
                continue

            hook_type = entry.get("type", "command")
            if hook_type not in HOOK_TYPES:
                errors.append(f"{prefix}: unknown type '{hook_type}'")
                continue

            if hook_type == "command":
                if not entry.get("bash") and not entry.get("command"):
                    errors.append(f"{prefix}: command hook needs 'bash' or 'command' field")

            elif hook_type == "http":
                url = entry.get("url")
                if not url:
                    errors.append(f"{prefix}: http hook needs 'url' field")
                else:
                    parsed = urlparse(url)
                    if parsed.scheme.lower() != "https":
                        errors.append(f"{prefix}: URL must use https:// scheme")
                    if parsed.username or parsed.password:
                        errors.append(f"{prefix}: URL must not contain embedded credentials")

    return errors
