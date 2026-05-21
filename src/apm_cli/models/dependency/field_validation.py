"""Final dependency-reference field validation helpers."""

from __future__ import annotations

import re

from apm_cli.utils.github_host import (
    is_artifactory_path,
    is_azure_devops_hostname,
    parse_artifactory_path,
)
from apm_cli.utils.path_security import validate_path_segments

from .reference import _NON_ADO_PATH_SEGMENT_RE


def _validate_final_repo_fields(cls, host, repo_url):
    """Validate repo_url and extract Azure DevOps organisation fields."""
    is_ado_final = host and is_azure_devops_hostname(host)
    if is_ado_final:
        if not re.match(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._\- ]+/[a-zA-Z0-9._\- ]+$", repo_url):
            raise ValueError(
                f"Invalid Azure DevOps repository format: {repo_url}. Expected 'org/project/repo'"
            )
        ado_parts = repo_url.split("/")
        validate_path_segments(repo_url, context="Azure DevOps repository path")
        return ado_parts[0], ado_parts[1], ado_parts[2]

    segments = repo_url.split("/")
    if len(segments) < 2:
        raise ValueError(f"Invalid repository format: {repo_url}. Expected 'user/repo'")
    if not all(re.match(_NON_ADO_PATH_SEGMENT_RE, s) for s in segments):
        raise ValueError(f"Invalid repository format: {repo_url}. Contains invalid characters")
    validate_path_segments(repo_url, context="repository path")
    for seg in segments:
        if any(seg.endswith(ext) for ext in cls.VIRTUAL_FILE_EXTENSIONS):
            raise ValueError(
                f"Invalid repository format: '{repo_url}' contains a virtual file extension. "
                f"Use the dict format with 'path:' for virtual packages in SSH/HTTPS URLs"
            )
    return None, None, None


def _extract_artifactory_prefix(dependency_str, host):
    """Extract the Artifactory VCS prefix from the original dependency string."""
    art_str = dependency_str.split("#")[0].split("@")[0]
    if "://" in art_str:
        art_str = art_str.split("://", 1)[1]
    art_segs = art_str.replace(f"{host}/", "", 1).split("/")
    if is_artifactory_path(art_segs):
        art_result = parse_artifactory_path(art_segs)
        if art_result:
            return art_result[0]
    return None
