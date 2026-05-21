"""Formatting helpers bound onto DependencyReference."""

from __future__ import annotations

import urllib.parse

from ...utils.github_host import default_host


def _to_apm_yml_entry(self):
    """Return the entry to store in apm.yml."""
    if self.is_insecure:
        host = self.host or default_host()
        entry = {"git": f"http://{host}/{self.repo_url}"}
        if self.reference:
            entry["ref"] = self.reference
        if self.alias:
            entry["alias"] = self.alias
        entry["allow_insecure"] = self.allow_insecure
        if self.skill_subset:
            entry["skills"] = sorted(self.skill_subset)
        return entry
    if self.skill_subset:
        entry = {"git": self.get_identity()}
        if self.reference:
            entry["ref"] = self.reference
        if self.alias:
            entry["alias"] = self.alias
        entry["skills"] = sorted(self.skill_subset)
        return entry
    return self.to_canonical()


def _to_github_url(self) -> str:
    """Convert to full repository URL."""
    if self.is_local and self.local_path:
        return self.local_path

    host = self.host or default_host()
    netloc = f"{host}:{self.port}" if self.port else host
    scheme = "http" if self.is_insecure else "https"

    if self.is_azure_devops():
        project = urllib.parse.quote(self.ado_project, safe="")
        repo = urllib.parse.quote(self.ado_repo, safe="")
        return f"https://{netloc}/{self.ado_organization}/{project}/_git/{repo}"
    if self.artifactory_prefix:
        return f"{scheme}://{netloc}/{self.artifactory_prefix}/{self.repo_url}"
    return f"{scheme}://{netloc}/{self.repo_url}"


def _to_clone_url(self) -> str:
    """Convert to a clone-friendly URL."""
    return self.to_github_url()


def _get_display_name(self) -> str:
    """Get display name for this dependency."""
    if self.alias:
        return self.alias
    if self.is_local and self.local_path:
        return self.local_path
    if self.is_virtual:
        return self.get_virtual_package_name()
    return self.repo_url


def _to_string(self) -> str:
    """String representation of the dependency reference."""
    if self.is_local and self.local_path:
        return self.local_path
    if self.host:
        host_label = f"{self.host}:{self.port}" if self.port else self.host
        result = (
            f"{host_label}/{self.artifactory_prefix}/{self.repo_url}"
            if self.artifactory_prefix
            else f"{host_label}/{self.repo_url}"
        )
    else:
        result = self.repo_url
    if self.virtual_path:
        result += f"/{self.virtual_path}"
    if self.reference:
        result += f"#{self.reference}"
    if self.alias:
        result += f"@{self.alias}"
    return result
