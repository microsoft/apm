"""Registry configuration precedence chain.

Merges registry name→URL maps from (highest to lowest precedence):
  1. apm-policy.yml  (policy-level mandates)
  2. project apm.yml (already parsed by APMPackage.registries)
  3. workspace ~/.apm/apm.yml
  4. ~/.apm/config.json

Only the URL is merged here; token resolution stays in auth.py.
The first (highest-precedence) definition of a name wins.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


def _load_yaml_registries(yaml_path: Path) -> dict[str, str]:
    """Return {name: url} from a YAML file's top-level ``registries:`` block.

    Silently returns an empty dict on any parse error so a broken
    workspace file never blocks a project install.
    """
    try:
        import yaml

        with yaml_path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            return {}
        raw = data.get("registries")
        if not isinstance(raw, dict):
            return {}
        result: dict[str, str] = {}
        for name, body in raw.items():
            if not isinstance(name, str) or not name.strip():
                continue
            if isinstance(body, dict):
                url = body.get("url")
                if isinstance(url, str) and url.strip():
                    result[name] = url.strip()
        return result
    except Exception:
        return {}


def _load_config_json_registries() -> dict[str, str]:
    """Return {name: url} from ~/.apm/config.json."""
    from ...config import _get_registries_section

    result: dict[str, str] = {}
    for name, body in _get_registries_section().items():
        if not isinstance(name, str) or not name.strip():
            continue
        if isinstance(body, dict):
            url = body.get("url")
            if isinstance(url, str) and url.strip():
                result[name] = url.strip()
    return result


def load_merged_registries(
    project_registries: Optional[dict[str, str]] = None,
    policy_registries: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    """Return merged registry name→URL map with precedence applied.

    Build order: config.json (lowest) → workspace apm.yml → project apm.yml
    → policy (highest). Later updates override earlier ones, so highest
    precedence lands last.
    """
    merged: dict[str, str] = {}

    # 4. config.json (lowest)
    merged.update(_load_config_json_registries())

    # 3. workspace ~/.apm/apm.yml
    workspace_yml = Path.home() / ".apm" / "apm.yml"
    if workspace_yml.exists():
        merged.update(_load_yaml_registries(workspace_yml))

    # 2. project apm.yml
    if project_registries:
        merged.update(project_registries)

    # 1. policy (highest)
    if policy_registries:
        merged.update(policy_registries)

    return merged
