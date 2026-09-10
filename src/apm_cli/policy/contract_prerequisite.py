"""Read-only native-contract prerequisite behind the policy discovery API."""

import os
import subprocess
from pathlib import Path

import yaml

from ..utils.git_env import get_git_executable, git_subprocess_env
from ..utils.yaml_io import load_yaml_str
from .discovery import PolicyFetchResult, _unverifiable_cache_pin
from .project_config import ProjectPolicyConfigError, parse_project_policy_hash_pin


def discover_contract_prerequisite(
    project_root: Path, *, manifest_data: dict | None = None
) -> PolicyFetchResult:
    """Require positive no-policy evidence without fetching or writing caches."""
    if os.environ.get("APM_POLICY_DISABLE") == "1":
        return PolicyFetchResult(outcome="disabled")
    try:
        if manifest_data is None:
            with (project_root / "apm.yml").open("rb") as stream:
                raw = stream.read(256 * 1024 + 1)
            if len(raw) > 256 * 1024:
                raise ValueError("Project manifest exceeds the contract read limit.")
            manifest_data = load_yaml_str(raw.decode("utf-8"))
        if not isinstance(manifest_data, dict):
            raise ValueError("Project manifest must be a mapping.")
        pin = parse_project_policy_hash_pin(manifest_data.get("policy"))
    except ProjectPolicyConfigError as exc:
        return PolicyFetchResult(outcome="hash_mismatch", source="apm.yml", error=str(exc))
    except (OSError, UnicodeError, ValueError, yaml.YAMLError):
        return PolicyFetchResult(
            outcome="malformed", source="apm.yml", error="Cannot read project policy configuration."
        )
    if pin is not None:
        return _unverifiable_cache_pin(pin.normalized, "apm.yml")
    if "policy" in manifest_data:
        return PolicyFetchResult(
            outcome="cache_miss_fetch_fail",
            source="apm.yml",
            error="Configured governance is unsupported by native contracts.",
        )

    def has_git_administration(parent: Path) -> bool:
        return (
            (parent / ".git").exists()
            or (parent / ".git").is_symlink()
            or (
                (parent / "HEAD").exists()
                and ((parent / "objects").exists() or (parent / "config").exists())
            )
        )

    if not any(has_git_administration(parent) for parent in (project_root, *project_root.parents)):
        return PolicyFetchResult(outcome="no_git_remote")
    try:
        result = subprocess.run(
            [get_git_executable(), "remote"],
            cwd=project_root,
            env=git_subprocess_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return PolicyFetchResult(
            outcome="cache_miss_fetch_fail",
            error="Cannot establish Git remote configuration offline.",
        )
    if result.returncode == 0 and not result.stdout.strip():
        return PolicyFetchResult(outcome="no_git_remote")
    return PolicyFetchResult(
        outcome="cache_miss_fetch_fail",
        error="Remote governance cannot be established by the offline native-contract profile.",
    )
