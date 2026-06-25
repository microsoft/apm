"""``apm scripts`` -- inspect, test, and scaffold lifecycle scripts.

Sub-commands:

* ``apm scripts``          -- list discovered scripts
* ``apm scripts test``     -- preview (dry-run) a synthetic event; --execute to run
* ``apm scripts init``     -- scaffold a starter script JSON file
* ``apm scripts validate`` -- check all script files for errors
* ``apm scripts trust``    -- trust .apm/scripts.json so its scripts run on install
* ``apm scripts untrust``  -- revoke trust for .apm/scripts.json
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
    "scripts": {
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
    help="Inspect, test, and scaffold lifecycle scripts.",
)
@click.pass_context
def scripts(ctx: click.Context) -> None:
    """List discovered lifecycle scripts when invoked without a sub-command."""
    if ctx.invoked_subcommand is not None:
        return

    from apm_cli.core.lifecycle_scripts import discover_scripts

    project_root = str(Path.cwd())
    entries = discover_scripts(project_root=project_root)

    if not entries:
        _rich_info("No lifecycle scripts discovered.", symbol="info")
        _rich_echo(
            "  Create one with: apm scripts init",
            style="dim",
        )
        return

    _rich_echo(
        f"{STATUS_SYMBOLS['check']} Discovered {len(entries)} script(s):\n",
        style="green",
    )

    try:
        from rich.table import Table

        from apm_cli.utils.console import _get_console

        console = _get_console()
        if console is None:
            raise ImportError("Rich console unavailable")

        table = Table(
            title="Lifecycle Scripts",
            show_header=True,
            header_style="bold cyan",
        )
        table.add_column("Event", style="bold white")
        table.add_column("Type", style="cyan")
        table.add_column("Target", style="white")
        table.add_column("Source", style="dim")

        for entry in entries:
            target = entry.url or entry.effective_command or "(none)"
            table.add_row(entry.event, entry.script_type, target, entry.source)

        console.print(table)
    except (ImportError, NameError):
        for entry in entries:
            target = entry.url or entry.effective_command or "(none)"
            click.echo(f"  {entry.event:20s} {entry.script_type:10s} {target} ({entry.source})")


@scripts.command(
    name="test",
    help="Dry-run a synthetic lifecycle event through discovered scripts.",
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
@click.option(
    "--execute",
    is_flag=True,
    help="Actually run the scripts (default is a non-executing dry-run).",
)
def scripts_test(event: str, verbose: bool, execute: bool) -> None:
    """Preview (or, with --execute, fire) a synthetic event through scripts."""
    from apm_cli.core.lifecycle_scripts import (
        LifecycleEvent,
        LifecycleScriptRunner,
        PackageInfo,
        discover_scripts,
    )

    project_root = str(Path.cwd())
    # `test` is an explicit, opt-in inspection of the developer's own repo,
    # so it is NOT subject to the install-time project-script trust gate (that
    # gate exists to stop scripts auto-firing on `apm install` of a clone).
    all_scripts = discover_scripts(project_root=project_root)
    runner = LifecycleScriptRunner(scripts=all_scripts, verbose=verbose, project_root=project_root)

    matching = runner.scripts_for_event(event)
    if not matching:
        _rich_warning(
            f"No scripts registered for '{event}'. Create one with: apm scripts init",
            symbol="warning",
        )
        return

    if not execute:
        _rich_info(
            f"Dry-run: '{event}' would fire {len(matching)} script(s) "
            "(no commands or requests are run).",
            symbol="gear",
        )
        for entry in matching:
            target = entry.url or entry.effective_command or "(none)"
            _rich_echo(f"  - {entry.script_type:8s} {target} ({entry.source})", style="dim")
        _rich_echo("")
        _rich_info("Re-run with --execute to actually run these scripts.", symbol="info")
        return

    _rich_info(
        f"Firing synthetic '{event}' event ({len(matching)} script(s))...",
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
        f"'{event}' event fired. Check ~/.apm/logs/scripts.log for output.",
        symbol="check",
    )


@scripts.command(
    name="init",
    help="Scaffold a starter script JSON file at .apm/scripts.json.",
)
@click.option("--force", is_flag=True, help="Overwrite if file already exists.")
def scripts_init(force: bool) -> None:
    """Create a starter script JSON file in the project."""
    apm_dir = Path.cwd() / ".apm"
    target_file = apm_dir / "scripts.json"

    if target_file.exists() and not force:
        _rich_warning(
            f"Script file already exists: {target_file.relative_to(Path.cwd())}",
            symbol="warning",
        )
        _rich_echo("  Use --force to overwrite.", style="dim")
        return

    apm_dir.mkdir(parents=True, exist_ok=True)

    content = json.dumps(_INIT_TEMPLATE, indent=2) + "\n"
    target_file.write_text(content, encoding="utf-8")

    _rich_success(
        f"Created script file: {target_file.relative_to(Path.cwd())}",
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
                "to add your scripts\n"
                "  2. Run [cyan]apm scripts validate[/cyan] to check for errors\n"
                "  3. Run [cyan]apm scripts test post-install[/cyan] to dry-run\n",
                title="Getting Started",
                style="cyan",
            )
        )
    except (ImportError, NameError):
        click.echo("Next steps:")
        click.echo(f"  1. Edit {target_file.relative_to(Path.cwd())} to add your scripts")
        click.echo("  2. Run `apm scripts validate` to check for errors")
        click.echo("  3. Run `apm scripts test post-install` to dry-run")


@scripts.command(
    name="validate",
    help="Validate all discovered script files for errors.",
)
def scripts_validate() -> None:
    """Check all script JSON files across discovery sources for errors."""
    from apm_cli.core.lifecycle_scripts import (
        _get_policy_scripts_dir,
        _get_project_scripts_file,
        _get_user_scripts_dir,
    )

    project_root = str(Path.cwd())
    dirs = [
        ("policy", _get_policy_scripts_dir()),
        ("user", _get_user_scripts_dir()),
    ]

    total_files = 0
    total_errors = 0
    total_scripts = 0

    def _process_file(json_file: Path, source: str) -> None:
        nonlocal total_files, total_errors, total_scripts
        total_files += 1
        errors = _validate_script_file(json_file, source)
        if errors:
            total_errors += len(errors)
            rel = json_file.relative_to(Path.cwd()) if source == "project" else json_file
            _rich_error(f"{rel}:", symbol="error")
            for err in errors:
                _rich_echo(f"    {err}", style="red")
        else:
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
                script_count = sum(
                    len(v) for v in data.get("scripts", {}).values() if isinstance(v, list)
                )
                total_scripts += script_count
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
    project_file = _get_project_scripts_file(project_root)
    if project_file.is_file():
        _process_file(project_file, "project")

    if total_files == 0:
        _rich_info("No script files found.", symbol="info")
        _rich_echo("  Create one with: apm scripts init", style="dim")
        return

    if total_errors > 0:
        _rich_error(
            f"{total_errors} error(s) in {total_files} file(s).",
            symbol="error",
        )
        sys.exit(1)
    else:
        _rich_success(
            f"All {total_files} script file(s) valid ({total_scripts} script(s) configured).",
            symbol="check",
        )


def _validate_script_file(path: Path, source: str) -> list[str]:
    """Validate a single script JSON file. Returns a list of error messages."""
    from apm_cli.core.lifecycle_scripts import (
        LIFECYCLE_EVENTS,
        SCRIPT_FILE_VERSION,
        SCRIPT_TYPES,
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
    elif version != SCRIPT_FILE_VERSION:
        errors.append(f"Unsupported version: {version} (expected {SCRIPT_FILE_VERSION})")

    scripts_dict = data.get("scripts")
    if scripts_dict is None:
        errors.append("Missing 'scripts' field")
        return errors

    if not isinstance(scripts_dict, dict):
        errors.append("'scripts' must be a JSON object")
        return errors

    for event_name, script_list in scripts_dict.items():
        if event_name not in LIFECYCLE_EVENTS:
            errors.append(f"Unknown event: '{event_name}'")
            continue

        if not isinstance(script_list, list):
            errors.append(f"'{event_name}' must be an array")
            continue

        for i, entry in enumerate(script_list):
            prefix = f"{event_name}[{i}]"

            if not isinstance(entry, dict):
                errors.append(f"{prefix}: must be a JSON object")
                continue

            script_type = entry.get("type", "command")
            if script_type not in SCRIPT_TYPES:
                errors.append(f"{prefix}: unknown type '{script_type}'")
                continue

            if script_type == "command":
                if not entry.get("bash") and not entry.get("command"):
                    errors.append(f"{prefix}: command script needs 'bash' or 'command' field")

            elif script_type == "http":
                url = entry.get("url")
                if not url:
                    errors.append(f"{prefix}: http script needs 'url' field")
                else:
                    parsed = urlparse(url)
                    if parsed.scheme.lower() != "https":
                        errors.append(f"{prefix}: URL must use https:// scheme")
                    if parsed.username or parsed.password:
                        errors.append(f"{prefix}: URL must not contain embedded credentials")

    return errors


@scripts.command(
    name="trust",
    help="Trust the project's .apm/scripts.json so its scripts run on install.",
)
def scripts_trust() -> None:
    """Record trust for the current contents of ``.apm/scripts.json``.

    Project scripts are skipped on ``apm install`` until trusted, because a
    cloned repository could otherwise run arbitrary commands. Trust is
    bound to the file's exact contents -- editing the scripts re-arms the
    gate.
    """
    from apm_cli.core.lifecycle_scripts import _get_project_scripts_file
    from apm_cli.core.script_trust import trust_project_scripts

    project_file = _get_project_scripts_file(str(Path.cwd()))
    if not project_file.is_file():
        _rich_warning(
            "No project scripts file found at .apm/scripts.json.",
            symbol="warning",
        )
        _rich_echo("  Create one with: apm scripts init", style="dim")
        return

    fingerprint = trust_project_scripts(project_file)
    if fingerprint is None:
        _rich_error("Could not read .apm/scripts.json to record trust.", symbol="error")
        sys.exit(1)

    _rich_warning(
        "Project scripts can run arbitrary commands during apm install/update/uninstall.",
        symbol="warning",
    )
    _rich_success(
        f"Trusted .apm/scripts.json ({fingerprint[:12]}...). Its scripts will now run.",
        symbol="check",
    )


@scripts.command(
    name="untrust",
    help="Revoke trust for the project's .apm/scripts.json.",
)
def scripts_untrust() -> None:
    """Revoke trust for ``.apm/scripts.json`` so its scripts stop running."""
    from apm_cli.core.lifecycle_scripts import _get_project_scripts_file
    from apm_cli.core.script_trust import untrust_project_scripts

    project_file = _get_project_scripts_file(str(Path.cwd()))
    removed = untrust_project_scripts(project_file)
    if removed:
        _rich_success(
            "Revoked trust for .apm/scripts.json. Its scripts will no longer run.",
            symbol="check",
        )
    else:
        _rich_info("Project scripts were not trusted; nothing to revoke.", symbol="info")
