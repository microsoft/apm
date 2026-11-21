"""Bootstrap owner for a minimal ``apm.yml`` (#3879).

The default config and the comment-annotated minimal manifest that
``apm init``, ``apm install`` (auto-bootstrap) and ``apm install --mcp``
(user manifest) write. Lives below both the commands and install layers so
neither imports the other to create a manifest. The rendered manifest is
parsed back through the manifest target declaration owner before it is
written.
"""

from __future__ import annotations

from pathlib import Path

from ..constants import APM_YML_FILENAME
from ..utils.git_env import get_git_executable


def auto_detect_description(project_name):
    """Auto-detect description from git repository or use default."""
    import subprocess

    try:
        # Try to get git repository description
        result = subprocess.run(
            [get_git_executable(), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            # We have a git repo, but description is typically not set
            # Just use a sensible default
            pass
    except Exception:
        pass
    return f"APM project for {project_name}"


def bootstrap_config(project_name):
    """Default config for an apm.yml auto-created by ``apm install``.

    Omits ``author``: whoever first runs install is not necessarily the
    project's author, so the local git identity must not be recorded.
    """
    return {
        "name": project_name,
        "version": "1.0.0",
        "description": auto_detect_description(project_name),
    }


def write_minimal_manifest(config, plugin=False, target_path=None):
    """Create minimal apm.yml file with auto-detected metadata.

    Args:
        config: dict with name, version, description keys and an optional
            author key (omitted for install auto-bootstrap).
            Optional 'target' key: CSV string of targets to pin.
        plugin: if True, include a devDependencies section.
        target_path: explicit file path to write (defaults to cwd/apm.yml).

    The rendered manifest is parsed back through the manifest target
    declaration owner first, so no scaffold or bootstrap writes a manifest
    APM would then reject.

    Raises:
        TargetRewriteError: APM would reject the rendered manifest; nothing
            is written.
    """
    # Create minimal apm.yml structure
    apm_yml_data = {
        "name": config["name"],
        "version": config["version"],
        "description": config["description"],
    }
    if "author" in config:
        apm_yml_data["author"] = config["author"]

    # Add targets field if present in config (plural list form -- canonical).
    # Older callers may still pass a singular CSV "target" string; honor that
    # for backwards compatibility but normalise on disk to plural list form.
    # These read the init config, not a manifest.
    # architecture-authority-exempt: bootstrap-writer
    if config.get("targets"):
        # architecture-authority-exempt: bootstrap-writer
        apm_yml_data["targets"] = list(config["targets"])
    # architecture-authority-exempt: bootstrap-writer
    elif config.get("target"):
        # architecture-authority-exempt: bootstrap-writer
        raw = config["target"]
        if isinstance(raw, list):
            apm_yml_data["targets"] = list(raw)
        else:
            apm_yml_data["targets"] = [t.strip() for t in str(raw).split(",") if t.strip()]

    apm_yml_data["dependencies"] = {"apm": [], "mcp": []}
    # Issue #887: scaffold with explicit consent for local content
    # deployment so day-2 audit doesn't surprise the maintainer with
    # an "includes not declared" advisory the moment they drop a
    # primitive in .apm/.  Override with an explicit path list to
    # gate what gets deployed.
    apm_yml_data["includes"] = "auto"

    if plugin:
        apm_yml_data["devDependencies"] = {"apm": []}

    apm_yml_data["scripts"] = {}

    from ..utils.yaml_io import yaml_to_str

    out_file = Path(target_path or APM_YML_FILENAME)
    content = yaml_to_str(apm_yml_data)

    # architecture-authority-exempt: bootstrap-writer
    if "targets" in apm_yml_data:
        from apm_cli.core.target_catalog import advertised_manifest_target_names

        # Insert comment before the targets: line
        accepted_targets = ", ".join(sorted(advertised_manifest_target_names()))
        targets_comment = (
            "# Which agent platforms to deploy to.\n"
            "# Resolution order: --target flag > this field > 'apm config set target'\n"
            "# > auto-detect from filesystem.\n"
            f"# Accepted values: {accepted_targets}\n"
        )
        content = content.replace("targets:", targets_comment + "targets:", 1)
    else:
        # Insert commented-out skeleton before dependencies:
        skeleton = (
            "# Which agent platforms to deploy to (uncomment to pin):\n"
            "# targets:\n"
            "#   - copilot\n"
            "#   - claude\n"
        )
        content = content.replace("dependencies:", skeleton + "\ndependencies:", 1)

    if plugin:
        # Scaffold the field without choosing a license for the author (#3799):
        # 'apm pack' records NOASSERTION in the SBOM until it is set.
        license_comment = (
            "# SPDX license for this package (set before publishing), for example:\n"
            "# license: MIT\n"
        )
        content = content.replace("dependencies:", license_comment + "\ndependencies:", 1)

    from .manifest_targets import verify_rewritten_manifest

    verify_rewritten_manifest(out_file, content)
    out_file.write_text(content, encoding="utf-8")
