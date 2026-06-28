"""``apm lifecycle`` -- inspect, test, and scaffold lifecycle scripts.

Sub-commands:

* ``apm lifecycle``          -- list discovered scripts
* ``apm lifecycle test``     -- preview (dry-run) a synthetic event; --execute to run
* ``apm lifecycle init``     -- inject a starter lifecycle: block into apm.yml
* ``apm lifecycle validate`` -- check all script files for errors
* ``apm lifecycle trust``    -- trust apm.yml lifecycle: so its scripts run on install
* ``apm lifecycle untrust``  -- revoke trust for apm.yml lifecycle:
"""

from __future__ import annotations

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


@click.group(
    invoke_without_command=True,
    help="Inspect, test, and scaffold lifecycle scripts.",
)
@click.pass_context
def lifecycle(ctx: click.Context) -> None:
    """List discovered lifecycle scripts when invoked without a sub-command."""
    if ctx.invoked_subcommand is not None:
        return

    from apm_cli.core.lifecycle_scripts import discover_scripts

    project_root = str(Path.cwd())
    entries = discover_scripts(project_root=project_root)

    if not entries:
        _rich_info("No lifecycle scripts discovered.", symbol="info")
        _rich_echo(
            "  Create one with: apm lifecycle init",
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


@lifecycle.command(
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
def lifecycle_test(event: str, verbose: bool, execute: bool) -> None:
    """Preview (or, with --execute, fire) a synthetic event through scripts."""
    from apm_cli.core.lifecycle_scripts import (
        LifecycleEvent,
        LifecycleScriptRunner,
        PackageInfo,
        _get_project_apm_yml,
        discover_scripts,
    )
    from apm_cli.core.script_trust import is_project_scripts_trusted

    project_root = str(Path.cwd())
    all_scripts = discover_scripts(project_root=project_root)
    runner = LifecycleScriptRunner(scripts=all_scripts, verbose=verbose, project_root=project_root)

    matching = runner.scripts_for_event(event)
    if not matching:
        _rich_warning(
            f"No scripts registered for '{event}'. Create one with: apm lifecycle init",
            symbol="warning",
        )
        return

    if not execute:
        project_file = _get_project_apm_yml(project_root)
        trusted = is_project_scripts_trusted(project_file)
        trust_label = "[trusted]" if trusted else "[untrusted -- run: apm lifecycle trust]"
        _rich_info(
            f"Dry-run: '{event}' would fire {len(matching)} script(s) "
            "(no commands or requests are run).",
            symbol="gear",
        )
        _rich_echo(f"  Trust status: {trust_label}", style="dim")
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

    for t in threads:
        t.join(timeout=15)

    _rich_success(
        f"'{event}' event fired. Check ~/.apm/logs/scripts.log for output.",
        symbol="check",
    )


@lifecycle.command(
    name="init",
    help="Inject a starter lifecycle: block into apm.yml.",
)
@click.option("--force", is_flag=True, help="Overwrite existing lifecycle: block if present.")
def lifecycle_init(force: bool) -> None:
    """Inject a starter lifecycle: block into the project apm.yml file."""
    from apm_cli.utils.yaml_io import dump_yaml, load_yaml

    target_file = Path.cwd() / "apm.yml"

    if not target_file.is_file():
        _rich_error(
            "No apm.yml found in the current directory.",
            symbol="error",
        )
        _rich_echo("  Run 'apm init' first to create apm.yml.", style="dim")
        sys.exit(1)

    try:
        data = load_yaml(target_file) or {}
    except Exception as exc:
        _rich_error(f"Cannot read apm.yml: {exc}", symbol="error")
        sys.exit(1)

    if "lifecycle" in data and not force:
        _rich_warning(
            "apm.yml already has a lifecycle: block.",
            symbol="warning",
        )
        _rich_echo("  Use --force to overwrite.", style="dim")
        return

    data["lifecycle"] = {
        "post-install": [
            {
                "type": "command",
                "description": "Example: set up local build deps",
                "command": "echo 'apm lifecycle: edit this entry'",
                "timeoutSec": 30,
            }
        ]
    }
    dump_yaml(data, target_file)

    _rich_success(
        "Injected lifecycle: block into apm.yml.",
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
                "  1. Edit [cyan]apm.yml[/cyan] to customise the lifecycle: block\n"
                "  2. Run [cyan]apm lifecycle validate[/cyan] to check for errors\n"
                "  3. Run [cyan]apm lifecycle test post-install[/cyan] to dry-run\n"
                "  4. Run [cyan]apm lifecycle trust[/cyan] to allow scripts to run\n",
                title="Getting Started",
                style="cyan",
            )
        )
    except (ImportError, NameError):
        click.echo("Next steps:")
        click.echo("  1. Edit apm.yml to customise the lifecycle: block")
        click.echo("  2. Run `apm lifecycle validate` to check for errors")
        click.echo("  3. Run `apm lifecycle test post-install` to dry-run")
        click.echo("  4. Run `apm lifecycle trust` to allow scripts to run")


@lifecycle.command(
    name="validate",
    help="Validate all discovered script files for errors.",
)
def lifecycle_validate() -> None:
    """Check all script files across discovery sources for errors."""
    from apm_cli.core.lifecycle_scripts import (
        _get_policy_scripts_dir,
        _get_project_apm_yml,
        _get_user_apm_yml,
    )

    project_root = str(Path.cwd())
    policy_dir = _get_policy_scripts_dir()

    total_files = 0
    total_errors = 0
    total_scripts = 0

    def _process_file(script_file: Path, source: str) -> None:
        nonlocal total_files, total_errors, total_scripts
        total_files += 1
        errors = _validate_script_file(script_file, source)
        if errors:
            total_errors += len(errors)
            rel = script_file.relative_to(Path.cwd()) if source == "project" else script_file
            _rich_error(f"{rel}:", symbol="error")
            for err in errors:
                _rich_echo(f"    {err}", style="red")
        else:
            try:
                data = _load_file_data(script_file, source)
                if source in ("project", "user"):
                    if isinstance(data, dict):
                        lifecycle = data.get("lifecycle", {})
                        if isinstance(lifecycle, dict):
                            script_count = sum(
                                len(v) for v in lifecycle.values() if isinstance(v, list)
                            )
                            total_scripts += script_count
                elif isinstance(data, dict):
                    script_count = sum(
                        len(v) for v in data.get("scripts", {}).values() if isinstance(v, list)
                    )
                    total_scripts += script_count
            except Exception:
                pass

    if policy_dir.is_dir():
        for json_file in sorted(policy_dir.glob("*.json")):
            if json_file.is_file():
                _process_file(json_file, "policy")

    user_yml = _get_user_apm_yml()
    if user_yml.is_file():
        _process_file(user_yml, "user")

    project_yml = _get_project_apm_yml(project_root)
    if project_yml.is_file():
        _process_file(project_yml, "project")

    if total_files == 0:
        _rich_info("No script files found.", symbol="info")
        _rich_echo("  Create one with: apm lifecycle init", style="dim")
        return

    if total_errors > 0:
        _rich_error(
            f"{total_errors} error(s) in {total_files} file(s).",
            symbol="error",
        )
        sys.exit(1)

    _rich_success(
        f"All {total_files} script file(s) valid ({total_scripts} script(s) configured).",
        symbol="check",
    )


def _load_file_data(path: Path, source: str) -> object:
    """Load raw data from a script file (JSON for policy, YAML for project/user)."""
    import json as _json

    if source in ("project", "user") or path.suffix in (".yml", ".yaml"):
        from apm_cli.utils.yaml_io import load_yaml

        return load_yaml(path)
    return _json.loads(path.read_text(encoding="utf-8"))


def _validate_script_file(path: Path, source: str) -> list[str]:
    """Validate a single script file. Returns a list of error messages."""
    import json as _json

    from apm_cli.core.lifecycle_scripts import (
        LIFECYCLE_EVENTS,
        SCRIPT_FILE_VERSION,
        SCRIPT_TYPES,
    )

    errors: list[str] = []

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as e:
        return [f"Cannot read file: {e}"]

    is_apm_yml = source in ("project", "user") or path.suffix in (".yml", ".yaml")
    if is_apm_yml:
        try:
            from apm_cli.utils.yaml_io import load_yaml

            data = load_yaml(path)
        except Exception as e:
            return [f"Invalid YAML: {e}"]
    else:
        try:
            data = _json.loads(raw_text)
        except _json.JSONDecodeError as e:
            return [f"Invalid JSON: {e}"]

    if not isinstance(data, dict):
        return ["Root must be a mapping object"]

    if is_apm_yml:
        lifecycle = data.get("lifecycle")
        if lifecycle is None:
            return []
        if not isinstance(lifecycle, dict):
            return ["lifecycle: must be a mapping object"]
        scripts_dict = lifecycle
    else:
        version = data.get("version")
        if version is None:
            errors.append("Missing 'version' field")
        elif version != SCRIPT_FILE_VERSION:
            errors.append(f"Unsupported version: {version} (expected {SCRIPT_FILE_VERSION})")

        scripts_dict_raw = data.get("scripts")
        if scripts_dict_raw is None:
            errors.append("Missing 'scripts' field")
            return errors
        if not isinstance(scripts_dict_raw, dict):
            errors.append("'scripts' must be a mapping object")
            return errors
        scripts_dict = scripts_dict_raw

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
                errors.append(f"{prefix}: must be a mapping object")
                continue

            script_type = entry.get("type")
            if script_type is None:
                script_type = "http" if entry.get("url") else "command"
            if script_type not in SCRIPT_TYPES:
                errors.append(f"{prefix}: unknown type '{script_type}'")
                continue

            if script_type == "command":
                if not entry.get("bash") and not entry.get("command") and not entry.get("run"):
                    errors.append(
                        f"{prefix}: command script needs 'bash', 'command', or 'run' field"
                    )

            if script_type == "http":
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


@lifecycle.command(
    name="trust",
    help="Trust the project apm.yml lifecycle: block so its scripts run on install.",
)
def lifecycle_trust() -> None:
    """Record trust for the current lifecycle: subtree of apm.yml.

    Project scripts are skipped on apm install until trusted, because a
    cloned repository could otherwise run arbitrary commands. Trust is
    bound to the lifecycle: subtree -- editing other keys does not revoke
    trust, but editing lifecycle: re-arms the gate.
    """
    from apm_cli.core.lifecycle_scripts import _get_project_apm_yml
    from apm_cli.core.script_trust import trust_project_scripts

    project_file = _get_project_apm_yml(str(Path.cwd()))
    if not project_file.is_file():
        _rich_warning(
            "No apm.yml found in the current directory.",
            symbol="warning",
        )
        _rich_echo(
            "  Create one with: apm init, then add lifecycle: with apm lifecycle init",
            style="dim",
        )
        return

    _rich_warning(
        "Project lifecycle scripts can run arbitrary commands during apm install/update/uninstall.",
        symbol="warning",
    )
    fingerprint = trust_project_scripts(project_file)
    if fingerprint is None:
        _rich_error(
            "Could not read apm.yml lifecycle: block to record trust.",
            symbol="error",
        )
        sys.exit(1)

    _rich_success(
        f"Trusted apm.yml lifecycle: ({fingerprint[:12]}...). Its scripts will now run.",
        symbol="check",
    )


@lifecycle.command(
    name="untrust",
    help="Revoke trust for the project apm.yml lifecycle: block.",
)
def lifecycle_untrust() -> None:
    """Revoke trust for the apm.yml lifecycle: block so its scripts stop running."""
    from apm_cli.core.lifecycle_scripts import _get_project_apm_yml
    from apm_cli.core.script_trust import untrust_project_scripts

    project_file = _get_project_apm_yml(str(Path.cwd()))
    removed = untrust_project_scripts(project_file)
    if removed:
        _rich_success(
            "Revoked trust for apm.yml lifecycle:. Its scripts will no longer run.",
            symbol="check",
        )
    else:
        _rich_info("Project lifecycle scripts were not trusted; nothing to revoke.", symbol="info")
