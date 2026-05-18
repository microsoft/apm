"""GitHub package downloader for APM dependencies."""

import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Union

from ...models.apm_package import (
    APMPackage,
    DependencyReference,
    GitReferenceType,
    PackageInfo,
    ResolvedReference,
)
from ...utils.yaml_io import yaml_to_str
from .class_ import _debug

_PROTOCOL_FALLBACK_DOCS_URL = (
    "https://microsoft.github.io/apm/guides/dependencies/#restoring-the-legacy-permissive-chain"
)


@dataclass(frozen=True, slots=True)
class ProgressCtx:
    """Bundled progress-reporting arguments to avoid wide signatures."""

    progress_task_id: object = None
    progress_obj: object = None
    verbose_callback: object = None


def _extract_description_from_frontmatter(file_content: bytes, default: str) -> str:
    """Return description from YAML frontmatter, or *default* on any failure."""
    try:
        content_str = file_content.decode("utf-8")
        if content_str.startswith("---\n"):
            end_idx = content_str.find("\n---\n", 4)
            if end_idx > 0:
                frontmatter = content_str[4:end_idx]
                for line in frontmatter.split("\n"):
                    if line.startswith("description:"):
                        return line.split(":", 1)[1].strip().strip("\"'")
    except Exception:
        pass
    return default


def download_virtual_file_package(
    self,
    dep_ref: DependencyReference,
    target_path: Path,
    progress_task_id=None,
    progress_obj=None,
) -> PackageInfo:
    """Download a single file as a virtual APM package.

    Creates a minimal APM package structure with the file placed in the appropriate
    .apm/ subdirectory based on its extension.

    Args:
        dep_ref: Dependency reference with virtual_path set
        target_path: Local path where virtual package should be created
        progress_task_id: Rich Progress task ID for progress updates
        progress_obj: Rich Progress object for progress updates

    Returns:
        PackageInfo: Information about the created virtual package

    Raises:
        ValueError: If the dependency is not a valid virtual file package
        RuntimeError: If download fails
    """
    if not dep_ref.is_virtual or not dep_ref.virtual_path:
        raise ValueError("Dependency must be a virtual file package")

    if not dep_ref.is_virtual_file():
        raise ValueError(
            f"Path '{dep_ref.virtual_path}' is not a valid individual file. "
            f"Must end with one of: {', '.join(DependencyReference.VIRTUAL_FILE_EXTENSIONS)}"
        )

    # Determine the ref to use
    ref = dep_ref.reference or "main"

    # Resolve the commit SHA cheaply BEFORE the file download. This is one
    # short HTTP call (Accept: application/vnd.github.sha returns just the
    # 40-char SHA in the body) and the result is propagated into PackageInfo
    # so the lockfile and per-dep header can render the SHA suffix instead
    # of just the ref name. On non-GitHub hosts or any failure this returns
    # None and we fall back to ref-name only -- the install never fails on
    # SHA resolution.
    resolved_commit = self._resolve_commit_sha_for_ref(dep_ref, ref)

    # Update progress - downloading
    if progress_obj and progress_task_id is not None:
        progress_obj.update(progress_task_id, completed=50, total=100)

    # Download the file content
    try:
        file_content = self.download_raw_file(dep_ref, dep_ref.virtual_path, ref)
    except RuntimeError as e:
        raise RuntimeError(f"Failed to download virtual package: {e}") from e

    # Update progress - processing
    if progress_obj and progress_task_id is not None:
        progress_obj.update(progress_task_id, completed=90, total=100)

    # Create target directory structure
    target_path.mkdir(parents=True, exist_ok=True)

    # Determine the subdirectory based on file extension
    subdirs = {
        ".prompt.md": "prompts",
        ".instructions.md": "instructions",
        ".chatmode.md": "chatmodes",
        ".agent.md": "agents",
    }

    subdir = None
    filename = dep_ref.virtual_path.split("/")[-1]
    for ext, dir_name in subdirs.items():
        if dep_ref.virtual_path.endswith(ext):
            subdir = dir_name
            break

    if not subdir:
        raise ValueError(f"Unknown file extension for {dep_ref.virtual_path}")

    # Create .apm structure
    apm_dir = target_path / ".apm" / subdir
    apm_dir.mkdir(parents=True, exist_ok=True)

    # Write the file
    file_path = apm_dir / filename
    file_path.write_bytes(file_content)

    # Generate minimal apm.yml
    package_name = dep_ref.get_virtual_package_name()

    # Try to extract description from file frontmatter
    description = _extract_description_from_frontmatter(
        file_content, f"Virtual package containing {filename}"
    )

    apm_yml_data = {
        "name": package_name,
        "version": "1.0.0",
        "description": description,
        "author": dep_ref.repo_url.split("/")[0],
    }
    apm_yml_content = yaml_to_str(apm_yml_data)

    apm_yml_path = target_path / "apm.yml"
    apm_yml_path.write_text(apm_yml_content, encoding="utf-8")

    # Create APMPackage object
    package = APMPackage(
        name=package_name,
        version="1.0.0",
        description=description,
        author=dep_ref.repo_url.split("/")[0],
        source=dep_ref.to_github_url(),
        package_path=target_path,
    )

    # Build the resolved reference. On non-GitHub hosts or SHA-resolve
    # failure the resolved_commit stays None and the suffix renders as
    # "#ref" only -- matching the existing subdirectory behavior in
    # _try_sparse_checkout / _download_subdirectory.
    ref_type = (
        GitReferenceType.COMMIT
        if re.match(r"^[a-f0-9]{40}$", ref.lower())
        else GitReferenceType.BRANCH
    )
    resolved_ref = ResolvedReference(
        original_ref=str(dep_ref.reference) if dep_ref.reference else ref,
        ref_name=ref,
        ref_type=ref_type,
        resolved_commit=resolved_commit,
    )

    # Return PackageInfo
    return PackageInfo(
        package=package,
        install_path=target_path,
        installed_at=datetime.now().isoformat(),
        dependency_ref=dep_ref,  # Store for canonical dependency string
        resolved_reference=resolved_ref,
    )


def _try_sparse_checkout(
    self,
    dep_ref: DependencyReference,
    temp_clone_path: Path,
    subdir_path: str,
    ref: str | None = None,
) -> bool:
    """Attempt sparse-checkout to download only a subdirectory (git 2.25+).

    Returns True on success. Falls back silently on failure.
    """

    try:
        temp_clone_path.mkdir(parents=True, exist_ok=True)

        # Resolve per-dependency token via AuthResolver.
        dep_token = self._resolve_dep_token(dep_ref)
        dep_auth_ctx = self._resolve_dep_auth_ctx(dep_ref)
        dep_auth_scheme = dep_auth_ctx.auth_scheme if dep_auth_ctx else "basic"

        # For ADO bearer, use the AuthContext git_env with header injection
        if dep_auth_scheme == "bearer" and dep_auth_ctx is not None:
            env = {**os.environ, **(dep_auth_ctx.git_env or {})}
        else:
            env = {**os.environ, **(self.git_env or {})}
        auth_url = self._build_repo_url(
            dep_ref.repo_url,
            use_ssh=False,
            dep_ref=dep_ref,
            token=dep_token,
            auth_scheme=dep_auth_scheme,
        )

        cmds = [
            ["git", "init"],
            ["git", "remote", "add", "origin", auth_url],
            ["git", "sparse-checkout", "init", "--cone"],
            ["git", "sparse-checkout", "set", subdir_path],
        ]
        fetch_cmd = ["git", "fetch", "origin"]
        fetch_cmd.append(ref or "HEAD")
        fetch_cmd.append("--depth=1")
        cmds.append(fetch_cmd)
        cmds.append(["git", "checkout", "FETCH_HEAD"])

        for cmd in cmds:
            result = subprocess.run(
                cmd,
                cwd=str(temp_clone_path),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=120,
            )
            if result.returncode != 0:
                _debug(f"Sparse-checkout step failed ({' '.join(cmd)}): {result.stderr.strip()}")
                return False

        return True
    except Exception as e:
        _debug(f"Sparse-checkout failed: {e}")
        return False


def download_subdirectory_package(
    self, dep_ref: DependencyReference, target_path: Path, progress_task_id=None, progress_obj=None
) -> PackageInfo:
    return _download_package_ops.download_subdirectory_package(
        self, dep_ref, target_path, progress_task_id, progress_obj
    )


def download_package(
    self,
    repo_ref: Union[str, "DependencyReference"],
    target_path: Path,
    ctx: "ProgressCtx | None" = None,
) -> PackageInfo:
    return _download_package_ops.download_package(self, repo_ref, target_path, ctx)


def _get_clone_progress_callback(self):
    """Get a progress callback for Git clone operations.

    Returns:
        Callable that can be used as progress callback for GitPython
    """

    def progress_callback(op_code, cur_count, max_count=None, message=""):
        """Progress callback for Git operations."""
        if max_count:
            percentage = int((cur_count / max_count) * 100)
            print(
                f"\r Cloning: {percentage}% ({cur_count}/{max_count}) {message}",
                end="",
                flush=True,
            )
        else:
            print(f"\r Cloning: {message} ({cur_count})", end="", flush=True)

    return progress_callback


from . import download_package_ops as _download_package_ops
