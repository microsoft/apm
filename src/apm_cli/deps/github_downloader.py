"""GitHub package downloader for APM dependencies."""

import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, Callable
import random
import re
import requests

import git
from git import Repo, RemoteProgress
from git.exc import GitCommandError, InvalidGitRepositoryError

from ..core.token_manager import GitHubTokenManager
from ..models.apm_package import (
    DependencyReference, 
    PackageInfo, 
    ResolvedReference, 
    GitReferenceType,
    PackageType,
    validate_apm_package,
    APMPackage
)
from ..utils.github_host import (
    build_https_clone_url, 
    build_ssh_url, 
    build_ado_https_clone_url,
    build_ado_ssh_url,
    build_ado_api_url,
    sanitize_token_url_in_message, 
    default_host,
    is_azure_devops_hostname,
    is_github_hostname
)


def normalize_collection_path(virtual_path: str) -> str:
    """Normalize a collection virtual path by stripping any existing extension.
    
    This allows users to specify collection dependencies with or without the extension:
      - owner/repo/collections/name (without extension)
      - owner/repo/collections/name.collection.yml (with extension)
    
    Args:
        virtual_path: The virtual path from the dependency reference
        
    Returns:
        str: The normalized path without .collection.yml/.collection.yaml suffix
    """
    for ext in ('.collection.yml', '.collection.yaml'):
        if virtual_path.endswith(ext):
            return virtual_path[:-len(ext)]
    return virtual_path


def _debug(message: str) -> None:
    """Print debug message if APM_DEBUG environment variable is set."""
    if os.environ.get('APM_DEBUG'):
        print(f"[DEBUG] {message}", file=sys.stderr)


class GitProgressReporter(RemoteProgress):
    """Report git clone progress to Rich Progress."""
    
    def __init__(self, progress_task_id=None, progress_obj=None, package_name=None):
        super().__init__()
        self.task_id = progress_task_id
        self.progress = progress_obj
        self.package_name = package_name  # Keep consistent name throughout download
        self.last_op = None
        self.disabled = False  # Flag to stop updates after download completes
    
    def update(self, op_code, cur_count, max_count=None, message=''):
        """Called by GitPython during clone operations."""
        if not self.progress or self.task_id is None or self.disabled:
            return
        
        # Keep the package name consistent - don't change description to git operations
        # This keeps the UI clean and scannable
        
        # Update progress bar naturally - let it reach 100%
        if max_count and max_count > 0:
            # Determinate progress (we have total count)
            self.progress.update(
                self.task_id,
                completed=cur_count,
                total=max_count
                # Note: We don't update description - keep the original package name
            )
        else:
            # Indeterminate progress (just show activity)
            self.progress.update(
                self.task_id,
                total=100,  # Set fake total for indeterminate tasks
                completed=min(cur_count, 100) if cur_count else 0
                # Note: We don't update description - keep the original package name
            )
        
        self.last_op = cur_count
    
    def _get_op_name(self, op_code):
        """Convert git operation code to human-readable name."""
        from git import RemoteProgress
        
        # Extract operation type from op_code
        if op_code & RemoteProgress.COUNTING:
            return "Counting objects"
        elif op_code & RemoteProgress.COMPRESSING:
            return "Compressing objects"
        elif op_code & RemoteProgress.WRITING:
            return "Writing objects"
        elif op_code & RemoteProgress.RECEIVING:
            return "Receiving objects"
        elif op_code & RemoteProgress.RESOLVING:
            return "Resolving deltas"
        elif op_code & RemoteProgress.FINDING_SOURCES:
            return "Finding sources"
        elif op_code & RemoteProgress.CHECKING_OUT:
            return "Checking out files"
        else:
            return "Cloning"


class GitHubPackageDownloader:
    """Downloads and validates APM packages from GitHub repositories."""
    
    def __init__(self):
        """Initialize the GitHub package downloader."""
        self.token_manager = GitHubTokenManager()
        self.git_env = self._setup_git_environment()
    
    def _setup_git_environment(self) -> Dict[str, Any]:
        """Set up Git environment with authentication using centralized token manager.
        
        Returns:
            Dict containing environment variables for Git operations
        """
        # Use centralized token management
        env = self.token_manager.setup_environment()
        
        # Get tokens for modules (APM package access)
        # GitHub: GITHUB_APM_PAT → GITHUB_TOKEN
        self.github_token = self.token_manager.get_token_for_purpose('modules', env)
        self.has_github_token = self.github_token is not None
        
        # Azure DevOps: ADO_APM_PAT
        self.ado_token = self.token_manager.get_token_for_purpose('ado_modules', env)
        self.has_ado_token = self.ado_token is not None
        
        _debug(f"Token setup: has_github_token={self.has_github_token}, has_ado_token={self.has_ado_token}")
        
        # Configure Git security settings
        env['GIT_TERMINAL_PROMPT'] = '0'
        env['GIT_ASKPASS'] = 'echo'  # Prevent interactive credential prompts
        env['GIT_CONFIG_NOSYSTEM'] = '1'
        env['GIT_CONFIG_GLOBAL'] = '/dev/null'
        
        return env
    
    def _resilient_get(self, url: str, headers: Dict[str, str], timeout: int = 30, max_retries: int = 3) -> requests.Response:
        """HTTP GET with retry on 429/503 and rate-limit header awareness (#171).
        
        Args:
            url: Request URL
            headers: HTTP headers
            timeout: Request timeout in seconds
            max_retries: Maximum retry attempts for transient failures
            
        Returns:
            requests.Response (caller should call .raise_for_status() as needed)
            
        Raises:
            requests.exceptions.RequestException: After all retries exhausted
        """
        last_exc = None
        for attempt in range(max_retries):
            try:
                response = requests.get(url, headers=headers, timeout=timeout)
                
                # Handle rate limiting
                if response.status_code in (429, 503):
                    retry_after = response.headers.get("Retry-After")
                    if retry_after:
                        try:
                            wait = min(float(retry_after), 60)
                        except (TypeError, ValueError):
                            # Retry-After may be an HTTP-date; fall back to exponential backoff
                            wait = min(2 ** attempt, 30) * (0.5 + random.random())
                    else:
                        wait = min(2 ** attempt, 30) * (0.5 + random.random())
                    _debug(f"Rate limited ({response.status_code}), retry in {wait:.1f}s (attempt {attempt + 1}/{max_retries})")
                    time.sleep(wait)
                    continue
                
                # Log rate limit proximity
                remaining = response.headers.get("X-RateLimit-Remaining")
                try:
                    if remaining and int(remaining) < 10:
                        _debug(f"GitHub API rate limit low: {remaining} requests remaining")
                except (TypeError, ValueError):
                    pass
                
                return response
            except requests.exceptions.ConnectionError as e:
                last_exc = e
                if attempt < max_retries - 1:
                    wait = min(2 ** attempt, 30) * (0.5 + random.random())
                    _debug(f"Connection error, retry in {wait:.1f}s (attempt {attempt + 1}/{max_retries})")
                    time.sleep(wait)
            except requests.exceptions.Timeout as e:
                last_exc = e
                if attempt < max_retries - 1:
                    _debug(f"Timeout, retrying (attempt {attempt + 1}/{max_retries})")
        
        if last_exc:
            raise last_exc
        raise requests.exceptions.RequestException(f"All {max_retries} attempts failed for {url}")
    
    def _sanitize_git_error(self, error_message: str) -> str:
        """Sanitize Git error messages to remove potentially sensitive authentication information.
        
        Args:
            error_message: Raw error message from Git operations
            
        Returns:
            str: Sanitized error message with sensitive data removed
        """
        import re
        
        # Remove any tokens that might appear in URLs for github hosts (format: https://token@host)
        # Sanitize for default host and common enterprise hosts via helper
        sanitized = sanitize_token_url_in_message(error_message, host=default_host())
        
        # Sanitize Azure DevOps URLs - both cloud (dev.azure.com) and any on-prem server
        # Use a generic pattern to catch https://token@anyhost format for all hosts
        # This catches: dev.azure.com, ado.company.com, tfs.internal.corp, etc.
        sanitized = re.sub(r'https://[^@\s]+@([^\s/]+)', r'https://***@\1', sanitized)
        
        # Remove any tokens that might appear as standalone values
        sanitized = re.sub(r'(ghp_|gho_|ghu_|ghs_|ghr_)[a-zA-Z0-9_]+', '***', sanitized)
        
        # Remove environment variable values that might contain tokens
        sanitized = re.sub(r'(GITHUB_TOKEN|GITHUB_APM_PAT|ADO_APM_PAT|GH_TOKEN|GITHUB_COPILOT_PAT)=[^\s]+', r'\1=***', sanitized)
        
        return sanitized

    def _build_repo_url(self, repo_ref: str, use_ssh: bool = False, dep_ref: DependencyReference = None) -> str:
        """Build the appropriate repository URL for cloning.
        
        Supports both GitHub and Azure DevOps URL formats:
        - GitHub: https://github.com/owner/repo.git
        - ADO: https://dev.azure.com/org/project/_git/repo
        
        Args:
            repo_ref: Repository reference in format "owner/repo" or "org/project/repo" for ADO
            use_ssh: Whether to use SSH URL for git operations
            dep_ref: Optional DependencyReference for ADO-specific URL building
            
        Returns:
            str: Repository URL suitable for git clone operations
        """
        # Use dep_ref.host if available (for ADO), otherwise fall back to instance or default
        if dep_ref and dep_ref.host:
            host = dep_ref.host
        else:
            host = getattr(self, 'github_host', None) or default_host()
        
        # Check if this is Azure DevOps (either via dep_ref or host detection)
        is_ado = (dep_ref and dep_ref.is_azure_devops()) or is_azure_devops_hostname(host)
        
        _debug(f"_build_repo_url: host={host}, is_ado={is_ado}, dep_ref={'present' if dep_ref else 'None'}, "
               f"ado_org={dep_ref.ado_organization if dep_ref else None}")
        
        if is_ado and dep_ref and dep_ref.ado_organization:
            # Use Azure DevOps URL builders with ADO-specific token
            if use_ssh:
                return build_ado_ssh_url(dep_ref.ado_organization, dep_ref.ado_project, dep_ref.ado_repo)
            elif self.ado_token:
                return build_ado_https_clone_url(
                    dep_ref.ado_organization, 
                    dep_ref.ado_project, 
                    dep_ref.ado_repo, 
                    token=self.ado_token,
                    host=host
                )
            else:
                return build_ado_https_clone_url(
                    dep_ref.ado_organization, 
                    dep_ref.ado_project, 
                    dep_ref.ado_repo,
                    host=host
                )
        else:
            # Determine if this host should receive a GitHub token
            is_github = is_github_hostname(host)
            if use_ssh:
                return build_ssh_url(host, repo_ref)
            elif is_github and self.github_token:
                # Only send GitHub tokens to GitHub hosts
                return build_https_clone_url(host, repo_ref, token=self.github_token)
            else:
                # Generic hosts: plain HTTPS, let git credential helpers handle auth
                return build_https_clone_url(host, repo_ref, token=None)
    
    def _clone_with_fallback(self, repo_url_base: str, target_path: Path, progress_reporter=None, dep_ref: DependencyReference = None, **clone_kwargs) -> Repo:
        """Attempt to clone a repository with fallback authentication methods.
        
        Uses authentication patterns appropriate for the platform:
        - GitHub: x-access-token format for private repos, SSH, or HTTPS
        - Azure DevOps: PAT-based authentication
        
        Args:
            repo_url_base: Base repository reference (owner/repo)
            target_path: Target path for cloning
            progress_reporter: GitProgressReporter instance for progress updates
            dep_ref: Optional DependencyReference for platform-specific URL building
            **clone_kwargs: Additional arguments for Repo.clone_from
            
        Returns:
            Repo: Successfully cloned repository
            
        Raises:
            RuntimeError: If all authentication methods fail
        """
        last_error = None
        is_ado = dep_ref and dep_ref.is_azure_devops()
        
        # Determine host type for auth decisions
        dep_host = dep_ref.host if dep_ref else None
        if dep_host:
            is_github = is_github_hostname(dep_host)
        else:
            # When no host is specified, default to GitHub behavior
            is_github = True
        is_generic = not is_ado and not is_github
        
        # Tokens are only valid for their matching host type
        has_token = self.ado_token if is_ado else (self.github_token if is_github else None)
        
        _debug(f"_clone_with_fallback: repo={repo_url_base}, is_ado={is_ado}, is_generic={is_generic}, has_token={has_token is not None}")
        
        # When APM has a token for this host, use the locked-down env (APM manages auth).
        # When no token is available, relax the env so git credential helpers (gh auth,
        # macOS Keychain, etc.) can provide credentials — regardless of host.
        if has_token:
            clone_env = self.git_env
        else:
            clone_env = {k: v for k, v in self.git_env.items()
                         if k not in ('GIT_ASKPASS', 'GIT_CONFIG_GLOBAL', 'GIT_CONFIG_NOSYSTEM')}
            clone_env['GIT_TERMINAL_PROMPT'] = '0'  # Still prevent interactive prompts
        
        # Method 1: Try authenticated HTTPS if token is available (GitHub/ADO only)
        if has_token:
            try:
                auth_url = self._build_repo_url(repo_url_base, use_ssh=False, dep_ref=dep_ref)
                _debug(f"Attempting clone with authenticated HTTPS (URL sanitized)")
                return Repo.clone_from(auth_url, target_path, env=clone_env, progress=progress_reporter, **clone_kwargs)
            except GitCommandError as e:
                last_error = e
                # Continue to next method
        
        # Method 2: Try SSH (works with SSH keys for any host)
        try:
            ssh_url = self._build_repo_url(repo_url_base, use_ssh=True, dep_ref=dep_ref)
            return Repo.clone_from(ssh_url, target_path, env=clone_env, progress=progress_reporter, **clone_kwargs)
        except GitCommandError as e:
            last_error = e
            # Continue to next method
        
        # Method 3: Try standard HTTPS (public repos, or git credential helper for generic hosts)
        try:
            https_url = self._build_repo_url(repo_url_base, use_ssh=False, dep_ref=dep_ref)
            return Repo.clone_from(https_url, target_path, env=clone_env, progress=progress_reporter, **clone_kwargs)
        except GitCommandError as e:
            last_error = e
        
        # All methods failed
        error_msg = f"Failed to clone repository {repo_url_base} using all available methods. "
        configured_host = os.environ.get("GITHUB_HOST", "")
        if is_ado and not self.has_ado_token:
            error_msg += "For private Azure DevOps repositories, set ADO_APM_PAT environment variable."
        elif is_generic:
            host_name = dep_host or "the target host"
            error_msg += (
                f"For private repositories on {host_name}, configure SSH keys or a git credential helper. "
                f"APM delegates authentication to git for non-GitHub/ADO hosts."
            )
        elif configured_host and dep_host and dep_host == configured_host and configured_host != "github.com":
            suggested = f"github.com/{repo_url_base}"
            if dep_ref and dep_ref.virtual_path:
                suggested += f"/{dep_ref.virtual_path}"
            error_msg += (
                f"GITHUB_HOST is set to '{configured_host}', so shorthand dependencies "
                f"(without a hostname) resolve against that host. "
                f"If this package lives on a different server (e.g., github.com), "
                f"use the full hostname in apm.yml: {suggested}"
            )
        elif not self.has_github_token:
            error_msg += "For private repositories, set GITHUB_APM_PAT or GITHUB_TOKEN environment variable, " \
                        "or ensure SSH keys are configured."
        else:
            error_msg += "Please check repository access permissions and authentication setup."
        
        if last_error:
            sanitized_error = self._sanitize_git_error(str(last_error))
            error_msg += f" Last error: {sanitized_error}"
        
        raise RuntimeError(error_msg)
    
    def resolve_git_reference(self, repo_ref: str) -> ResolvedReference:
        """Resolve a Git reference (branch/tag/commit) to a specific commit SHA.
        
        Args:
            repo_ref: Repository reference string (e.g., "user/repo#branch")
            
        Returns:
            ResolvedReference: Resolved reference with commit SHA
            
        Raises:
            ValueError: If the reference format is invalid
            RuntimeError: If Git operations fail
        """
        # Parse the repository reference
        try:
            dep_ref = DependencyReference.parse(repo_ref)
        except ValueError as e:
            raise ValueError(f"Invalid repository reference '{repo_ref}': {e}")
        
        # Default to main branch if no reference specified
        ref = dep_ref.reference or "main"
        
        # Pre-analyze the reference type to determine the best approach
        is_likely_commit = re.match(r'^[a-f0-9]{7,40}$', ref.lower()) is not None
        
        # Create a temporary directory for Git operations
        temp_dir = None
        try:
            import tempfile
            temp_dir = Path(tempfile.mkdtemp())
            
            if is_likely_commit:
                # For commit SHAs, clone full repository first, then checkout the commit
                try:
                    # Ensure host is set for enterprise repos     
                    repo = self._clone_with_fallback(dep_ref.repo_url, temp_dir, progress_reporter=None, dep_ref=dep_ref)
                    commit = repo.commit(ref)
                    ref_type = GitReferenceType.COMMIT
                    resolved_commit = commit.hexsha
                    ref_name = ref
                except Exception as e:
                    sanitized_error = self._sanitize_git_error(str(e))
                    raise ValueError(f"Could not resolve commit '{ref}' in repository {dep_ref.repo_url}: {sanitized_error}")
            else:
                # For branches and tags, try shallow clone first
                try:
                    # Try to clone with specific branch/tag first
                    repo = self._clone_with_fallback(
                        dep_ref.repo_url,
                        temp_dir,
                        progress_reporter=None,
                        dep_ref=dep_ref,
                        depth=1,
                        branch=ref
                    )
                    ref_type = GitReferenceType.BRANCH  # Could be branch or tag
                    resolved_commit = repo.head.commit.hexsha
                    ref_name = ref

                except GitCommandError:
                    # If branch/tag clone fails, try full clone and resolve reference
                    try:
                        repo = self._clone_with_fallback(dep_ref.repo_url, temp_dir, progress_reporter=None, dep_ref=dep_ref)

                        # Try to resolve the reference
                        try:
                            # Try as branch first
                            try:
                                branch = repo.refs[f"origin/{ref}"]
                                ref_type = GitReferenceType.BRANCH
                                resolved_commit = branch.commit.hexsha
                                ref_name = ref
                            except IndexError:
                                # Try as tag
                                try:
                                    tag = repo.tags[ref]
                                    ref_type = GitReferenceType.TAG
                                    resolved_commit = tag.commit.hexsha
                                    ref_name = ref
                                except IndexError:
                                    raise ValueError(f"Reference '{ref}' not found in repository {dep_ref.repo_url}")

                        except Exception as e:
                            sanitized_error = self._sanitize_git_error(str(e))
                            raise ValueError(f"Could not resolve reference '{ref}' in repository {dep_ref.repo_url}: {sanitized_error}")

                    except GitCommandError as e:
                        # Check if this might be a private repository access issue
                        if "Authentication failed" in str(e) or "remote: Repository not found" in str(e):
                            error_msg = f"Failed to clone repository {dep_ref.repo_url}. "
                            if not self.has_github_token:
                                error_msg += "This might be a private repository that requires authentication. " \
                                           "Please set GITHUB_APM_PAT or GITHUB_TOKEN environment variable."
                            else:
                                error_msg += "Authentication failed. Please check your GitHub token permissions."
                            raise RuntimeError(error_msg)
                        else:
                            sanitized_error = self._sanitize_git_error(str(e))
                            raise RuntimeError(f"Failed to clone repository {dep_ref.repo_url}: {sanitized_error}")
                    
        finally:
            # Clean up temporary directory
            if temp_dir and temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
        
        return ResolvedReference(
            original_ref=repo_ref,
            ref_type=ref_type,
            resolved_commit=resolved_commit,
            ref_name=ref_name
        )
    
    def download_raw_file(self, dep_ref: DependencyReference, file_path: str, ref: str = "main") -> bytes:
        """Download a single file from repository (GitHub or Azure DevOps).
        
        Args:
            dep_ref: Parsed dependency reference
            file_path: Path to file within the repository (e.g., "prompts/code-review.prompt.md")
            ref: Git reference (branch, tag, or commit SHA). Defaults to "main"
            
        Returns:
            bytes: File content
            
        Raises:
            RuntimeError: If download fails or file not found
        """
        host = dep_ref.host or default_host()
        
        # Check if this is Azure DevOps
        if dep_ref.is_azure_devops():
            return self._download_ado_file(dep_ref, file_path, ref)
        
        # GitHub API
        return self._download_github_file(dep_ref, file_path, ref)
    
    def _download_ado_file(self, dep_ref: DependencyReference, file_path: str, ref: str = "main") -> bytes:
        """Download a file from Azure DevOps repository.
        
        Args:
            dep_ref: Parsed dependency reference with ADO-specific fields
            file_path: Path to file within the repository
            ref: Git reference (branch, tag, or commit SHA)
            
        Returns:
            bytes: File content
        """
        import base64
        
        # Validate required ADO fields before proceeding
        if not all([dep_ref.ado_organization, dep_ref.ado_project, dep_ref.ado_repo]):
            raise ValueError(
                f"Invalid Azure DevOps dependency reference: missing organization, project, or repo. "
                f"Got: org={dep_ref.ado_organization}, project={dep_ref.ado_project}, repo={dep_ref.ado_repo}"
            )
        
        host = dep_ref.host or "dev.azure.com"
        api_url = build_ado_api_url(
            dep_ref.ado_organization,
            dep_ref.ado_project,
            dep_ref.ado_repo,
            file_path,
            ref,
            host
        )
        
        # Set up authentication headers - ADO uses Basic auth with PAT
        headers = {}
        if self.ado_token:
            # ADO uses Basic auth: username can be empty, password is the PAT
            auth = base64.b64encode(f":{self.ado_token}".encode()).decode()
            headers['Authorization'] = f'Basic {auth}'
        
        try:
            response = self._resilient_get(api_url, headers=headers, timeout=30)
            response.raise_for_status()
            return response.content
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                # Try fallback branches
                if ref not in ["main", "master"]:
                    raise RuntimeError(f"File not found: {file_path} at ref '{ref}' in {dep_ref.repo_url}")
                
                fallback_ref = "master" if ref == "main" else "main"
                fallback_url = build_ado_api_url(
                    dep_ref.ado_organization,
                    dep_ref.ado_project,
                    dep_ref.ado_repo,
                    file_path,
                    fallback_ref,
                    host
                )
                
                try:
                    response = self._resilient_get(fallback_url, headers=headers, timeout=30)
                    response.raise_for_status()
                    return response.content
                except requests.exceptions.HTTPError:
                    raise RuntimeError(
                        f"File not found: {file_path} in {dep_ref.repo_url} "
                        f"(tried refs: {ref}, {fallback_ref})"
                    )
            elif e.response.status_code == 401 or e.response.status_code == 403:
                error_msg = f"Authentication failed for Azure DevOps {dep_ref.repo_url}. "
                if not self.ado_token:
                    error_msg += "Please set ADO_APM_PAT with an Azure DevOps PAT with Code (Read) scope."
                else:
                    error_msg += "Please check your Azure DevOps PAT permissions."
                raise RuntimeError(error_msg)
            else:
                raise RuntimeError(f"Failed to download {file_path}: HTTP {e.response.status_code}")
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Network error downloading {file_path}: {e}")
    
    def _download_github_file(self, dep_ref: DependencyReference, file_path: str, ref: str = "main") -> bytes:
        """Download a file from GitHub repository.
        
        Args:
            dep_ref: Parsed dependency reference
            file_path: Path to file within the repository
            ref: Git reference (branch, tag, or commit SHA)
            
        Returns:
            bytes: File content
        """
        host = dep_ref.host or default_host()
        
        # Parse owner/repo from repo_url
        owner, repo = dep_ref.repo_url.split('/', 1)
        
        # Build GitHub API URL - format differs by host type
        if host == "github.com":
            # GitHub.com: https://api.github.com/repos/owner/repo/contents/path
            api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}?ref={ref}"
        elif host.lower().endswith(".ghe.com"):
            # GitHub Enterprise Cloud Data Residency: https://api.{subdomain}.ghe.com/repos/owner/repo/contents/path
            api_url = f"https://api.{host}/repos/{owner}/{repo}/contents/{file_path}?ref={ref}"
        else:
            # GitHub Enterprise Server: https://{host}/api/v3/repos/owner/repo/contents/path
            api_url = f"https://{host}/api/v3/repos/{owner}/{repo}/contents/{file_path}?ref={ref}"
        
        # Set up authentication headers
        headers = {
            'Accept': 'application/vnd.github.v3.raw'  # Returns raw content directly
        }
        if self.github_token:
            headers['Authorization'] = f'token {self.github_token}'
        
        # Try to download with the specified ref
        try:
            response = self._resilient_get(api_url, headers=headers, timeout=30)
            response.raise_for_status()
            return response.content
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                # Try fallback branches if the specified ref fails
                if ref not in ["main", "master"]:
                    # If original ref failed, don't try fallbacks - it might be a specific version
                    raise RuntimeError(f"File not found: {file_path} at ref '{ref}' in {dep_ref.repo_url}")
                
                # Try the other default branch
                fallback_ref = "master" if ref == "main" else "main"
                
                # Build fallback API URL
                if host == "github.com":
                    fallback_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}?ref={fallback_ref}"
                elif host.lower().endswith(".ghe.com"):
                    fallback_url = f"https://api.{host}/repos/{owner}/{repo}/contents/{file_path}?ref={fallback_ref}"
                else:
                    fallback_url = f"https://{host}/api/v3/repos/{owner}/{repo}/contents/{file_path}?ref={fallback_ref}"
                
                try:
                    response = self._resilient_get(fallback_url, headers=headers, timeout=30)
                    response.raise_for_status()
                    return response.content
                except requests.exceptions.HTTPError:
                    raise RuntimeError(
                        f"File not found: {file_path} in {dep_ref.repo_url} "
                        f"(tried refs: {ref}, {fallback_ref})"
                    )
            elif e.response.status_code == 401 or e.response.status_code == 403:
                # Token may lack SSO/SAML authorization for this org.
                # Retry without auth — the repo might be public.
                # Applies to github.com and GHES (custom domains can have public repos).
                # Excluded: *.ghe.com (Enterprise Cloud Data Residency has no public repos).
                if self.github_token and not host.lower().endswith(".ghe.com"):
                    try:
                        unauth_headers = {'Accept': 'application/vnd.github.v3.raw'}
                        response = self._resilient_get(api_url, headers=unauth_headers, timeout=30)
                        response.raise_for_status()
                        return response.content
                    except requests.exceptions.HTTPError:
                        pass  # Fall through to the original error
                error_msg = f"Authentication failed for {dep_ref.repo_url} (file: {file_path}, ref: {ref}). "
                if not self.github_token:
                    error_msg += "This might be a private repository. Please set GITHUB_APM_PAT or GITHUB_TOKEN."
                elif self.github_token and not host.lower().endswith(".ghe.com"):
                    error_msg += (
                        "Both authenticated and unauthenticated access were attempted. "
                        "The repository may be private, or your token may lack SSO/SAML authorization for this organization."
                    )
                else:
                    error_msg += "Please check your GitHub token permissions."
                raise RuntimeError(error_msg)
            else:
                raise RuntimeError(f"Failed to download {file_path}: HTTP {e.response.status_code}")
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Network error downloading {file_path}: {e}")
    
    def validate_virtual_package_exists(self, dep_ref: DependencyReference) -> bool:
        """Validate that a virtual package (file, collection, or subdirectory) exists on GitHub.
        
        Supports:
        - Virtual files: owner/repo/path/file.prompt.md
        - Collections: owner/repo/collections/name (checks for .collection.yml)
        - Subdirectory packages: owner/repo/path/subdir (checks for apm.yml, SKILL.md, or plugin.json)
        
        Args:
            dep_ref: Parsed dependency reference for virtual package
            
        Returns:
            bool: True if the package exists and is accessible, False otherwise
        """
        if not dep_ref.is_virtual:
            raise ValueError("Can only validate virtual packages with this method")
        
        ref = dep_ref.reference or "main"
        file_path = dep_ref.virtual_path
        
        # For collections, check for .collection.yml file
        if dep_ref.is_virtual_collection():
            file_path = f"{dep_ref.virtual_path}.collection.yml"
            try:
                self.download_raw_file(dep_ref, file_path, ref)
                return True
            except RuntimeError:
                return False
        
        # For virtual files, check the file directly
        if dep_ref.is_virtual_file():
            try:
                self.download_raw_file(dep_ref, file_path, ref)
                return True
            except RuntimeError:
                return False
        
        # For subdirectory packages: apm.yml or SKILL.md confirm the type;
        # plugin.json confirms a Claude plugin; README.md is a last-resort
        # signal that the directory exists (any directory that follows the
        # Claude plugin spec may have none of the above).
        if dep_ref.is_virtual_subdirectory():
            # Try apm.yml first
            try:
                self.download_raw_file(dep_ref, f"{dep_ref.virtual_path}/apm.yml", ref)
                return True
            except RuntimeError:
                pass
            
            # Try SKILL.md
            try:
                self.download_raw_file(dep_ref, f"{dep_ref.virtual_path}/SKILL.md", ref)
                return True
            except RuntimeError:
                pass
            
            # Try plugin.json at various Claude plugin locations
            plugin_locations = [
                f"{dep_ref.virtual_path}/plugin.json",                                    # Root
                f"{dep_ref.virtual_path}/.github/plugin/plugin.json",                     # GitHub Copilot format
                f"{dep_ref.virtual_path}/.claude-plugin/plugin.json",                     # Claude format
            ]

            for plugin_path in plugin_locations:
                try:
                    self.download_raw_file(dep_ref, plugin_path, ref)
                    return True
                except RuntimeError:
                    continue

            # Last resort: README.md — any well-formed directory should have one.
            # A directory that follows the Claude plugin spec (agents/, commands/,
            # skills/ …) with no manifest files is still a valid plugin.
            try:
                self.download_raw_file(dep_ref, f"{dep_ref.virtual_path}/README.md", ref)
                return True
            except RuntimeError:
                pass
        
        # Fallback: try to download the file directly
        try:
            self.download_raw_file(dep_ref, file_path, ref)
            return True
        except RuntimeError:
            return False
    
    def download_virtual_file_package(self, dep_ref: DependencyReference, target_path: Path, progress_task_id=None, progress_obj=None) -> PackageInfo:
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
            raise ValueError(f"Path '{dep_ref.virtual_path}' is not a valid individual file. "
                           f"Must end with one of: {', '.join(DependencyReference.VIRTUAL_FILE_EXTENSIONS)}")
        
        # Determine the ref to use
        ref = dep_ref.reference or "main"
        
        # Update progress - downloading
        if progress_obj and progress_task_id is not None:
            progress_obj.update(progress_task_id, completed=50, total=100)
        
        # Download the file content
        try:
            file_content = self.download_raw_file(dep_ref, dep_ref.virtual_path, ref)
        except RuntimeError as e:
            raise RuntimeError(f"Failed to download virtual package: {e}")
        
        # Update progress - processing
        if progress_obj and progress_task_id is not None:
            progress_obj.update(progress_task_id, completed=90, total=100)
        
        # Create target directory structure
        target_path.mkdir(parents=True, exist_ok=True)
        
        # Determine the subdirectory based on file extension
        subdirs = {
            '.prompt.md': 'prompts',
            '.instructions.md': 'instructions',
            '.chatmode.md': 'chatmodes',
            '.agent.md': 'agents'
        }
        
        subdir = None
        filename = dep_ref.virtual_path.split('/')[-1]
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
        description = f"Virtual package containing {filename}"
        try:
            content_str = file_content.decode('utf-8')
            # Simple frontmatter parsing (YAML between --- markers)
            if content_str.startswith('---\n'):
                end_idx = content_str.find('\n---\n', 4)
                if end_idx > 0:
                    frontmatter = content_str[4:end_idx]
                    # Look for description field
                    for line in frontmatter.split('\n'):
                        if line.startswith('description:'):
                            description = line.split(':', 1)[1].strip().strip('"\'')
                            break
        except Exception:
            # If frontmatter parsing fails, use default description
            pass
        
        apm_yml_content = f"""name: {package_name}
version: 1.0.0
description: {description}
author: {dep_ref.repo_url.split('/')[0]}
"""
        
        apm_yml_path = target_path / "apm.yml"
        apm_yml_path.write_text(apm_yml_content, encoding='utf-8')
        
        # Create APMPackage object
        package = APMPackage(
            name=package_name,
            version="1.0.0",
            description=description,
            author=dep_ref.repo_url.split('/')[0],
            source=dep_ref.to_github_url(),
            package_path=target_path
        )
        
        # Return PackageInfo
        return PackageInfo(
            package=package,
            install_path=target_path,
            installed_at=datetime.now().isoformat(),
            dependency_ref=dep_ref  # Store for canonical dependency string
        )
    
    def download_collection_package(self, dep_ref: DependencyReference, target_path: Path, progress_task_id=None, progress_obj=None) -> PackageInfo:
        """Download a collection as a virtual APM package.
        
        Downloads the collection manifest, then fetches all referenced files and
        organizes them into the appropriate .apm/ subdirectories.
        
        Args:
            dep_ref: Dependency reference with virtual_path pointing to collection
            target_path: Local path where virtual package should be created
            progress_task_id: Rich Progress task ID for progress updates
            progress_obj: Rich Progress object for progress updates
            
        Returns:
            PackageInfo: Information about the created virtual package
            
        Raises:
            ValueError: If the dependency is not a valid collection package
            RuntimeError: If download fails
        """
        if not dep_ref.is_virtual or not dep_ref.virtual_path:
            raise ValueError("Dependency must be a virtual collection package")
        
        if not dep_ref.is_virtual_collection():
            raise ValueError(f"Path '{dep_ref.virtual_path}' is not a valid collection path")
        
        # Determine the ref to use
        ref = dep_ref.reference or "main"
        
        # Update progress - starting
        if progress_obj and progress_task_id is not None:
            progress_obj.update(progress_task_id, completed=10, total=100)
        
        # Normalize virtual_path by stripping .collection.yml/.yaml suffix if already present
        # This allows users to specify either:
        #   - owner/repo/collections/name (without extension)
        #   - owner/repo/collections/name.collection.yml (with extension)
        virtual_path_base = normalize_collection_path(dep_ref.virtual_path)
        
        # Extract collection name from normalized path (e.g., "collections/project-planning" -> "project-planning")
        collection_name = virtual_path_base.split('/')[-1]
        
        # Build collection manifest path - try .yml first, then .yaml as fallback
        collection_manifest_path = f"{virtual_path_base}.collection.yml"
        
        # Download the collection manifest
        try:
            manifest_content = self.download_raw_file(dep_ref, collection_manifest_path, ref)
        except RuntimeError as e:
            # Try .yaml extension as fallback
            if ".collection.yml" in str(e):
                collection_manifest_path = f"{virtual_path_base}.collection.yaml"
                try:
                    manifest_content = self.download_raw_file(dep_ref, collection_manifest_path, ref)
                except RuntimeError:
                    raise RuntimeError(f"Collection manifest not found: {virtual_path_base}.collection.yml (also tried .yaml)")
            else:
                raise RuntimeError(f"Failed to download collection manifest: {e}")
        
        # Parse the collection manifest
        from .collection_parser import parse_collection_yml
        
        try:
            manifest = parse_collection_yml(manifest_content)
        except (ValueError, Exception) as e:
            raise RuntimeError(f"Invalid collection manifest '{collection_name}': {e}")
        
        # Create target directory structure
        target_path.mkdir(parents=True, exist_ok=True)
        
        # Download all items from the collection
        downloaded_count = 0
        failed_items = []
        total_items = len(manifest.items)
        
        for idx, item in enumerate(manifest.items):
            # Update progress for each item
            if progress_obj and progress_task_id is not None:
                progress_percent = 20 + int((idx / total_items) * 70)  # 20% to 90%
                progress_obj.update(progress_task_id, completed=progress_percent, total=100)
            
            try:
                # Download the file
                item_content = self.download_raw_file(dep_ref, item.path, ref)
                
                # Determine subdirectory based on item kind
                subdir = item.subdirectory
                
                # Create the subdirectory
                apm_subdir = target_path / ".apm" / subdir
                apm_subdir.mkdir(parents=True, exist_ok=True)
                
                # Write the file
                filename = item.path.split('/')[-1]
                file_path = apm_subdir / filename
                file_path.write_bytes(item_content)
                
                downloaded_count += 1
                
            except RuntimeError as e:
                # Log the failure but continue with other items
                failed_items.append(f"{item.path} ({e})")
                continue
        
        # Check if we downloaded at least some items
        if downloaded_count == 0:
            error_msg = f"Failed to download any items from collection '{collection_name}'"
            if failed_items:
                error_msg += f". Failures:\n  - " + "\n  - ".join(failed_items)
            raise RuntimeError(error_msg)
        
        # Generate apm.yml with collection metadata
        package_name = dep_ref.get_virtual_package_name()
        
        apm_yml_content = f"""name: {package_name}
version: 1.0.0
description: {manifest.description}
author: {dep_ref.repo_url.split('/')[0]}
"""
        
        # Add tags if present
        if manifest.tags:
            apm_yml_content += f"\ntags:\n"
            for tag in manifest.tags:
                apm_yml_content += f"  - {tag}\n"
        
        apm_yml_path = target_path / "apm.yml"
        apm_yml_path.write_text(apm_yml_content, encoding='utf-8')
        
        # Create APMPackage object
        package = APMPackage(
            name=package_name,
            version="1.0.0",
            description=manifest.description,
            author=dep_ref.repo_url.split('/')[0],
            source=dep_ref.to_github_url(),
            package_path=target_path
        )
        
        # Log warnings for failed items if any
        if failed_items:
            import warnings
            warnings.warn(
                f"Collection '{collection_name}' installed with {downloaded_count}/{manifest.item_count} items. "
                f"Failed items: {len(failed_items)}"
            )
        
        # Return PackageInfo
        return PackageInfo(
            package=package,
            install_path=target_path,
            installed_at=datetime.now().isoformat(),
            dependency_ref=dep_ref  # Store for canonical dependency string
        )
    
    def _try_sparse_checkout(self, dep_ref: DependencyReference, temp_clone_path: Path, subdir_path: str, ref: str = None) -> bool:
        """Attempt sparse-checkout to download only a subdirectory (git 2.25+).

        Returns True on success. Falls back silently on failure.
        """
        import subprocess
        try:
            temp_clone_path.mkdir(parents=True, exist_ok=True)
            env = {**os.environ, **(self.git_env or {})}
            auth_url = self._build_repo_url(dep_ref.repo_url, use_ssh=False, dep_ref=dep_ref)

            cmds = [
                ['git', 'init'],
                ['git', 'remote', 'add', 'origin', auth_url],
                ['git', 'sparse-checkout', 'init', '--cone'],
                ['git', 'sparse-checkout', 'set', subdir_path],
            ]
            fetch_cmd = ['git', 'fetch', 'origin']
            fetch_cmd.append(ref or 'HEAD')
            fetch_cmd.append('--depth=1')
            cmds.append(fetch_cmd)
            cmds.append(['git', 'checkout', 'FETCH_HEAD'])

            for cmd in cmds:
                result = subprocess.run(
                    cmd, cwd=str(temp_clone_path), env=env,
                    capture_output=True, text=True, timeout=120,
                )
                if result.returncode != 0:
                    _debug(f"Sparse-checkout step failed ({' '.join(cmd)}): {result.stderr.strip()}")
                    return False

            return True
        except Exception as e:
            _debug(f"Sparse-checkout failed: {e}")
            return False

    def download_subdirectory_package(self, dep_ref: DependencyReference, target_path: Path, progress_task_id=None, progress_obj=None) -> PackageInfo:
        """Download a subdirectory from a repo as an APM package.
        
        Used for Claude Skills or APM packages nested in monorepos.
        Clones the repo, extracts the subdirectory, and cleans up.
        
        Args:
            dep_ref: Dependency reference with virtual_path set to subdirectory
            target_path: Local path where package should be created
            progress_task_id: Rich Progress task ID for progress updates
            progress_obj: Rich Progress object for progress updates
            
        Returns:
            PackageInfo: Information about the downloaded package
            
        Raises:
            ValueError: If the dependency is not a valid subdirectory package
            RuntimeError: If download or validation fails
        """
        if not dep_ref.is_virtual or not dep_ref.virtual_path:
            raise ValueError("Dependency must be a virtual subdirectory package")
        
        if not dep_ref.is_virtual_subdirectory():
            raise ValueError(f"Path '{dep_ref.virtual_path}' is not a valid subdirectory package")
        
        # Use user-specified ref, or None to use repo's default branch
        ref = dep_ref.reference  # None if not specified
        subdir_path = dep_ref.virtual_path
        
        # Update progress - starting
        if progress_obj and progress_task_id is not None:
            progress_obj.update(progress_task_id, completed=10, total=100)
        
        # Clone to a temporary directory first
        import tempfile
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_clone_path = Path(temp_dir) / "repo"
            
            # Update progress - cloning
            if progress_obj and progress_task_id is not None:
                progress_obj.update(progress_task_id, completed=20, total=100)
            
            # Phase 4 (#171): Try sparse-checkout first (git 2.25+), fall back to full clone
            sparse_ok = self._try_sparse_checkout(dep_ref, temp_clone_path, subdir_path, ref)
            
            if not sparse_ok:
                # Full clone fallback
                if temp_clone_path.exists():
                    shutil.rmtree(temp_clone_path)
                
                package_display_name = subdir_path.split('/')[-1]
                progress_reporter = GitProgressReporter(progress_task_id, progress_obj, package_display_name) if progress_task_id and progress_obj else None
                
                # Detect if ref is a commit SHA (can't be used with --branch in shallow clones)
                is_commit_sha = ref and re.match(r'^[a-f0-9]{7,40}$', ref) is not None
                
                clone_kwargs = {
                    'dep_ref': dep_ref,
                }
                if is_commit_sha:
                    # For commit SHAs, clone without checkout then checkout the specific commit.
                    # Shallow clone doesn't support fetching by arbitrary SHA.
                    clone_kwargs['no_checkout'] = True
                else:
                    clone_kwargs['depth'] = 1
                    if ref:
                        clone_kwargs['branch'] = ref
                
                try:
                    self._clone_with_fallback(
                        dep_ref.repo_url,
                        temp_clone_path,
                        progress_reporter=progress_reporter,
                        **clone_kwargs
                    )
                except Exception as e:
                    raise RuntimeError(f"Failed to clone repository: {e}") from e
                
                if is_commit_sha:
                    try:
                        repo_obj = Repo(temp_clone_path)
                        repo_obj.git.checkout(ref)
                    except Exception as e:
                        raise RuntimeError(f"Failed to checkout commit {ref}: {e}") from e
                
                # Disable progress reporter after clone
                if progress_reporter:
                    progress_reporter.disabled = True
            
            # Update progress - extracting subdirectory
            if progress_obj and progress_task_id is not None:
                progress_obj.update(progress_task_id, completed=70, total=100)
            
            # Check if subdirectory exists
            source_subdir = temp_clone_path / subdir_path
            if not source_subdir.exists():
                raise RuntimeError(f"Subdirectory '{subdir_path}' not found in repository")
            
            if not source_subdir.is_dir():
                raise RuntimeError(f"Path '{subdir_path}' is not a directory")
            
            # Create target directory
            target_path.mkdir(parents=True, exist_ok=True)
            
            # If target exists and has content, remove it
            if target_path.exists() and any(target_path.iterdir()):
                shutil.rmtree(target_path)
                target_path.mkdir(parents=True, exist_ok=True)
            
            # Copy subdirectory contents to target
            for item in source_subdir.iterdir():
                src = source_subdir / item.name
                dst = target_path / item.name
                if src.is_dir():
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
            
            # Capture commit SHA before temp dir is destroyed
            try:
                repo = Repo(temp_clone_path)
                resolved_commit = repo.head.commit.hexsha
            except Exception:
                resolved_commit = "unknown"
            
            # Update progress - validating
            if progress_obj and progress_task_id is not None:
                progress_obj.update(progress_task_id, completed=90, total=100)
        
        # Validate the extracted package (after temp dir is cleaned up)
        validation_result = validate_apm_package(target_path)
        if not validation_result.is_valid:
            error_msgs = "; ".join(validation_result.errors)
            raise RuntimeError(f"Subdirectory is not a valid APM package or Claude Skill: {error_msgs}")
        
        # Get the resolved reference for metadata
        resolved_ref = ResolvedReference(
            original_ref=ref or "default",
            ref_name=ref or "default",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit=resolved_commit
        )

        # For plugins without an explicit version, stamp with the short commit SHA.
        package = validation_result.package
        if (
            validation_result.package_type == PackageType.MARKETPLACE_PLUGIN
            and package.version == "0.0.0"
            and resolved_commit != "unknown"
        ):
            short_sha = resolved_commit[:7]
            package.version = short_sha
            apm_yml_path = target_path / "apm.yml"
            if apm_yml_path.exists():
                import yaml as _yaml
                with open(apm_yml_path, "r", encoding="utf-8") as _f:
                    _data = _yaml.safe_load(_f) or {}
                _data["version"] = short_sha
                with open(apm_yml_path, "w", encoding="utf-8") as _f:
                    _yaml.dump(_data, _f, default_flow_style=False, sort_keys=False)
        
        # Update progress - complete
        if progress_obj and progress_task_id is not None:
            progress_obj.update(progress_task_id, completed=100, total=100)
        
        return PackageInfo(
            package=package,
            install_path=target_path,
            resolved_reference=resolved_ref,
            installed_at=datetime.now().isoformat(),
            dependency_ref=dep_ref,
            package_type=validation_result.package_type
        )
    
    def download_package(
        self, 
        repo_ref: str, 
        target_path: Path,
        progress_task_id=None,
        progress_obj=None
    ) -> PackageInfo:
        """Download a GitHub repository and validate it as an APM package.
        
        For virtual packages (individual files or collections), creates a minimal
        package structure instead of cloning the full repository.
        
        Args:
            repo_ref: Repository reference string (e.g., "user/repo#branch" or "user/repo/path/file.prompt.md")
            target_path: Local path where package should be downloaded
            progress_task_id: Rich Progress task ID for progress updates
            progress_obj: Rich Progress object for progress updates
            
        Returns:
            PackageInfo: Information about the downloaded package
            
        Raises:
            ValueError: If the repository reference is invalid
            RuntimeError: If download or validation fails
        """
        # Parse the repository reference
        try:
            dep_ref = DependencyReference.parse(repo_ref)
        except ValueError as e:
            raise ValueError(f"Invalid repository reference '{repo_ref}': {e}")
        
        # Handle virtual packages differently
        if dep_ref.is_virtual:
            if dep_ref.is_virtual_file():
                # Individual file virtual package
                return self.download_virtual_file_package(dep_ref, target_path, progress_task_id, progress_obj)
            elif dep_ref.is_virtual_collection():
                # Collection virtual package
                return self.download_collection_package(dep_ref, target_path, progress_task_id, progress_obj)
            elif dep_ref.is_virtual_subdirectory():
                # Subdirectory package (e.g., Claude Skill in a monorepo)
                return self.download_subdirectory_package(dep_ref, target_path, progress_task_id, progress_obj)
            else:
                raise ValueError(f"Unknown virtual package type for {dep_ref.virtual_path}")
        
        # Regular package download (existing logic)
        # Resolve the Git reference to get specific commit
        resolved_ref = self.resolve_git_reference(repo_ref)
        
        # Create target directory if it doesn't exist
        target_path.mkdir(parents=True, exist_ok=True)
        
        # If directory already exists and has content, remove it
        if target_path.exists() and any(target_path.iterdir()):
            shutil.rmtree(target_path)
            target_path.mkdir(parents=True, exist_ok=True)
        
        # Store progress reporter so we can disable it after clone
        progress_reporter = None
        package_display_name = dep_ref.repo_url.split('/')[-1] if '/' in dep_ref.repo_url else dep_ref.repo_url
        
        try:
            # Clone the repository using fallback authentication methods
            # Use shallow clone for performance if we have a specific commit
            if resolved_ref.ref_type == GitReferenceType.COMMIT:
                # For commits, we need to clone and checkout the specific commit
                progress_reporter = GitProgressReporter(progress_task_id, progress_obj, package_display_name) if progress_task_id and progress_obj else None
                repo = self._clone_with_fallback(
                    dep_ref.repo_url, 
                    target_path, 
                    progress_reporter=progress_reporter,
                    dep_ref=dep_ref
                )
                repo.git.checkout(resolved_ref.resolved_commit)
            else:
                # For branches and tags, we can use shallow clone
                progress_reporter = GitProgressReporter(progress_task_id, progress_obj, package_display_name) if progress_task_id and progress_obj else None
                repo = self._clone_with_fallback(
                    dep_ref.repo_url,
                    target_path,
                    progress_reporter=progress_reporter,
                    dep_ref=dep_ref,
                    depth=1,
                    branch=resolved_ref.ref_name
                )
            
            # Disable progress reporter to prevent late git updates
            if progress_reporter:
                progress_reporter.disabled = True
            
            # Remove .git directory to save space and prevent treating as a Git repository
            git_dir = target_path / ".git"
            if git_dir.exists():
                shutil.rmtree(git_dir, ignore_errors=True)
                
        except GitCommandError as e:
            # Check if this might be a private repository access issue
            if "Authentication failed" in str(e) or "remote: Repository not found" in str(e):
                error_msg = f"Failed to clone repository {dep_ref.repo_url}. "
                if not self.has_github_token:
                    error_msg += "This might be a private repository that requires authentication. " \
                               "Please set GITHUB_APM_PAT or GITHUB_TOKEN environment variable."
                else:
                    error_msg += "Authentication failed. Please check your GitHub token permissions."
                raise RuntimeError(error_msg)
            else:
                sanitized_error = self._sanitize_git_error(str(e))
                raise RuntimeError(f"Failed to clone repository {dep_ref.repo_url}: {sanitized_error}")
        except RuntimeError:
            # Re-raise RuntimeError from _clone_with_fallback
            raise
        
        # Validate the downloaded package
        validation_result = validate_apm_package(target_path)
        if not validation_result.is_valid:
            # Clean up on validation failure
            if target_path.exists():
                shutil.rmtree(target_path, ignore_errors=True)
            
            error_msg = f"Invalid APM package {dep_ref.repo_url}:\n"
            for error in validation_result.errors:
                error_msg += f"  - {error}\n"
            raise RuntimeError(error_msg.strip())
        
        # Load the APM package metadata
        if not validation_result.package:
            raise RuntimeError(f"Package validation succeeded but no package metadata found for {dep_ref.repo_url}")
        
        package = validation_result.package
        package.source = dep_ref.to_github_url()
        package.resolved_commit = resolved_ref.resolved_commit

        # For plugins without an explicit version, use the short commit SHA so the
        # lock file and conflict detection have a meaningful, stable version string.
        if (
            validation_result.package_type == PackageType.MARKETPLACE_PLUGIN
            and package.version == "0.0.0"
            and resolved_ref.resolved_commit
        ):
            short_sha = resolved_ref.resolved_commit[:7]
            package.version = short_sha
            # Keep the synthesized apm.yml in sync
            apm_yml_path = target_path / "apm.yml"
            if apm_yml_path.exists():
                import yaml as _yaml
                with open(apm_yml_path, "r", encoding="utf-8") as _f:
                    _data = _yaml.safe_load(_f) or {}
                _data["version"] = short_sha
                with open(apm_yml_path, "w", encoding="utf-8") as _f:
                    _yaml.dump(_data, _f, default_flow_style=False, sort_keys=False)
        
        # Create and return PackageInfo
        return PackageInfo(
            package=package,
            install_path=target_path,
            resolved_reference=resolved_ref,
            installed_at=datetime.now().isoformat(),
            dependency_ref=dep_ref,  # Store for canonical dependency string
            package_type=validation_result.package_type  # Track if APM, Claude Skill, or Hybrid
        )
    
    def _get_clone_progress_callback(self):
        """Get a progress callback for Git clone operations.
        
        Returns:
            Callable that can be used as progress callback for GitPython
        """
        def progress_callback(op_code, cur_count, max_count=None, message=''):
            """Progress callback for Git operations."""
            if max_count:
                percentage = int((cur_count / max_count) * 100)
                print(f"\r🚀 Cloning: {percentage}% ({cur_count}/{max_count}) {message}", end='', flush=True)
            else:
                print(f"\r🚀 Cloning: {message} ({cur_count})", end='', flush=True)
        
        return progress_callback