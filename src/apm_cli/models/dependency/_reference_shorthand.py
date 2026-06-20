"""Shorthand / virtual-package detection mixin for ``DependencyReference``.

These classmethods/staticmethods are composed onto
:class:`~apm_cli.models.dependency.reference.DependencyReference` via mixin
inheritance, so ``cls`` binds to ``DependencyReference`` at call time and
cross-method calls resolve through the MRO. Nothing here imports the composed
class, so the package stays free of import cycles.
"""

import urllib.parse
from typing import TYPE_CHECKING

from ...utils.github_host import (
    is_artifactory_path,
    is_azure_devops_hostname,
    is_github_hostname,
    is_gitlab_hostname,
    is_supported_git_host,
    maybe_raise_bare_fqdn_github_gitlab_conflict,
    unsupported_host_error,
)
from ...utils.path_security import (
    PathTraversalError,
    validate_path_segments,
)
from ..validation import InvalidVirtualPackageExtensionError

if TYPE_CHECKING:
    from .reference import DependencyReference


class _ReferenceShorthandMixin:
    """Virtual-package detection + GitLab/Artifactory boundary heuristics."""

    @classmethod
    def virtual_suffix_is_installable_shape(cls, virtual_path: str) -> bool:
        """Return whether *virtual_path* matches APM virtual package shape rules.

        Used for GitLab direct host/path shorthand: a repo boundary is accepted
        only when the remaining suffix would be a valid virtual path (file,
        collection, or extension-less subdirectory), matching the rules applied
        in :meth:`_detect_virtual_package` for the tail segments.
        """
        if not virtual_path or not virtual_path.strip():
            return False
        v = virtual_path.strip().strip("/")
        try:
            validate_path_segments(v, context="virtual path")
        except PathTraversalError:
            return False
        if "/collections/" in v or v.startswith("collections/"):
            return True
        if any(v.endswith(ext) for ext in cls.VIRTUAL_FILE_EXTENSIONS):
            return True
        last = v.split("/")[-1]
        return "." not in last

    @classmethod
    def split_gitlab_direct_shorthand_parts(
        cls, package: str
    ) -> tuple[str, list[str], str | None] | None:
        """If *package* is bare host/path shorthand, return (host, path_segments, ref_str).

        Returns ``None`` for ``https://``, ``git@``, or non-GitLab-class hosts.
        """
        s = package.strip()
        ref_out: str | None = None
        if "#" in s:
            s, r = s.rsplit("#", 1)
            s = s.strip()
            r = r.strip()
            ref_out = r if r else None
        maybe_raise_bare_fqdn_github_gitlab_conflict(package)
        if s.startswith(("git@", "https://", "http://", "ssh://", "//")):
            return None
        if "/" not in s:
            return None
        parts = s.split("/")
        host_cand = parts[0]
        if "." not in host_cand:
            return None
        segs = [p for p in parts[1:] if p]
        if len(segs) < 1:
            return None
        if not is_supported_git_host(host_cand) or not is_gitlab_hostname(host_cand):
            return None
        return (host_cand, segs, ref_out)

    @classmethod
    def needs_gitlab_direct_shorthand_probing(
        cls, package: str, dep_ref: "DependencyReference"
    ) -> bool:
        """True when install should probe left-to-right repo boundaries (GitLab only)."""
        if dep_ref.is_local:
            return False
        if dep_ref.is_virtual:
            return False
        sp = cls.split_gitlab_direct_shorthand_parts(package)
        if not sp:
            return False
        _host, segs, _ref = sp
        return len(segs) >= 3

    @classmethod
    def iter_gitlab_direct_shorthand_boundary_candidates(cls, path_segments: list[str]):
        """Yield (repo_url, virtual_suffix) for k=2..n-1 (earliest k first)."""
        n = len(path_segments)
        if n < 3:
            return
        for k in range(2, n):
            repo = "/".join(path_segments[:k])
            suffix = "/".join(path_segments[k:])
            if cls.virtual_suffix_is_installable_shape(suffix):
                yield repo, suffix

    @classmethod
    def from_gitlab_shorthand_probe(
        cls,
        host: str,
        repo_url: str,
        virtual_path: str,
        reference: str | None,
    ) -> "DependencyReference":
        """Build a virtual dependency ref for a resolved GitLab shorthand probe."""
        return cls(
            repo_url=repo_url,
            host=host,
            reference=reference,
            virtual_path=virtual_path,
            is_virtual=True,
        )

    @classmethod
    def from_artifactory_boundary_probe(
        cls,
        host: str,
        prefix: str,
        owner: str,
        repo: str,
        virtual_path: str | None,
        reference: str | None,
    ) -> "DependencyReference":
        """Build a dependency ref for a resolved Artifactory boundary probe."""
        return cls(
            repo_url=f"{owner}/{repo}",
            host=host,
            reference=reference,
            virtual_path=virtual_path,
            is_virtual=bool(virtual_path),
            artifactory_prefix=prefix,
        )

    @classmethod
    def _gitlab_shorthand_repo_segment_count(
        cls,
        path_segments: list[str],
        has_virtual_ext: bool,
        has_collection: bool,
    ) -> int:
        """Return how many segments after the host belong to the GitLab project path.

        GitLab allows nested groups; unlike GitHub's fixed ``owner/repo``, the
        project slug may span 3+ segments. Virtual package shorthand must not
        chop a nested group path after two segments.

        Shorthand cannot disambiguate every deep namespace; ambiguous cases use
        object form with ``git:`` + ``path:`` in ``apm.yml``.

        This does **not** split extension-less paths (e.g. ``.../registry/pkg``)
        into repo + virtual: that would mis-parse valid 5+ segment project
        paths; use ``parse_from_dict`` with an explicit ``path`` for those.
        """
        n = len(path_segments)
        if n < 2:
            return n

        if has_collection and "collections" in path_segments:
            coll_idx = path_segments.index("collections")
            if coll_idx >= 2:
                return coll_idx
            return n

        if has_virtual_ext:
            for idx, seg in enumerate(path_segments):
                if idx >= 2 and seg in cls._GITLAB_VIRTUAL_ROOT_SEGMENTS:
                    return idx
            # 3-segment paths keep owner/repo; 4+ segment paths reserve the
            # first three for the (possibly nested-group) project slug.
            return 3 if n >= 4 else 2

        return n

    @classmethod
    def _bare_shorthand_repo_segment_count(cls, path_segments: list[str]) -> int:
        """Return how many leading segments belong to the repo path for bare shorthand.

        For ``owner/repo[/...]`` shorthand without an FQDN, the default is 2
        segments (GitHub convention).  When registry-only mode is active, the
        proxy may be fronting a host that allows nested namespaces (GitLab
        subgroups) -- parse defaults to **all-as-repo** so the deterministic
        boundary probe in :mod:`apm_cli.install.artifactory_resolver` can
        rebuild the dependency reference at the proxy-verified split.

        The only parse-time inference kept is **structural**: a path whose
        last segment ends in a virtual file extension
        (``.prompt.md``/``.instructions.md``/``.chatmode.md``/``.agent.md``)
        is by shape a virtual file dep -- the file is the last segment and
        the repo is everything before it.  This is not a directory-marker
        heuristic; the file extension is the type.  The shallower boundary
        (when the file lives under a known directory like ``prompts/``) is
        settled by the probe, not by a marker list.
        """
        n = len(path_segments)
        if n < 3:
            return 2

        from ...deps.registry_proxy import is_enforce_only

        if not is_enforce_only():
            return 2

        if any(path_segments[-1].endswith(ext) for ext in cls.VIRTUAL_FILE_EXTENSIONS):
            return n - 1
        return n

    @classmethod
    def _detect_virtual_package(cls, dependency_str: str):
        """Detect whether *dependency_str* refers to a virtual package.

        Returns:
            (is_virtual_package, virtual_path, validated_host)
        """
        # Temporarily remove reference for path segment counting
        temp_str = dependency_str
        if "#" in temp_str:
            temp_str = temp_str.rsplit("#", 1)[0]

        is_virtual_package = False
        virtual_path = None
        validated_host = None

        if temp_str.lower().startswith(("git@", "https://", "http://", "ssh://")):
            return is_virtual_package, virtual_path, validated_host

        check_str = temp_str

        if "/" in check_str:
            first_segment = check_str.split("/")[0]

            if "." in first_segment:
                test_url = f"https://{check_str}"
                try:
                    parsed = urllib.parse.urlparse(test_url)
                    hostname = parsed.hostname

                    if hostname and is_supported_git_host(hostname):
                        validated_host = hostname
                        path_parts = parsed.path.lstrip("/").split("/")
                        if len(path_parts) >= 2:
                            check_str = "/".join(check_str.split("/")[1:])
                    else:
                        raise ValueError(unsupported_host_error(hostname or first_segment))
                except (ValueError, AttributeError) as e:
                    if isinstance(e, ValueError) and "Invalid Git host" in str(e):
                        raise
                    raise ValueError(unsupported_host_error(first_segment)) from e
            elif check_str.startswith("gh/"):
                check_str = "/".join(check_str.split("/")[1:])

        path_segments = [seg for seg in check_str.split("/") if seg]

        # Azure DevOps ``_git`` segment is a URL marker, not part of the
        # org/project/repo path -- strip it before counting and slicing so
        # both the base-segment count and the virtual suffix are computed
        # against the real path.
        is_ado = validated_host is not None and is_azure_devops_hostname(validated_host)
        if is_ado and "_git" in path_segments:
            git_idx = path_segments.index("_git")
            path_segments = path_segments[:git_idx] + path_segments[git_idx + 1 :]

        min_base_segments = cls._virtual_min_base_segments(path_segments, validated_host)
        min_virtual_segments = min_base_segments + 1

        if len(path_segments) >= min_virtual_segments:
            is_virtual_package = True
            virtual_path = "/".join(path_segments[min_base_segments:])
            cls._validate_detected_virtual_path(virtual_path)

        return is_virtual_package, virtual_path, validated_host

    @classmethod
    def _virtual_min_base_segments(
        cls, path_segments: list[str], validated_host: str | None
    ) -> int:
        """Return the count of leading segments forming the base repo path.

        Encapsulates the per-host-class boundary rules (ADO / Artifactory /
        GitLab / generic FQDN / bare shorthand) used by
        :meth:`_detect_virtual_package`. ``path_segments`` must already have any
        Azure DevOps ``_git`` marker stripped by the caller.
        """
        is_ado = validated_host is not None and is_azure_devops_hostname(validated_host)
        is_generic_host = (
            validated_host is not None
            and not is_github_hostname(validated_host)
            and not is_azure_devops_hostname(validated_host)
        )
        is_gitlab_host = validated_host is not None and is_gitlab_hostname(validated_host)

        # Detect Artifactory VCS paths (artifactory/{repo-key}/{owner}/{repo})
        is_artifactory = is_generic_host and is_artifactory_path(path_segments)

        if is_ado:
            from ...utils.github_host import is_visualstudio_legacy_hostname

            # *.visualstudio.com encodes org in the subdomain; path is proj/repo (2 parts).
            # dev.azure.com encodes org as the first path segment; path is org/proj/repo (3 parts).
            if validated_host and is_visualstudio_legacy_hostname(validated_host):
                return 2
            return 3
        if is_artifactory:
            # Artifactory: artifactory/{repo-key}/{owner}/{repo}
            return 4
        if is_generic_host:
            has_virtual_ext = any(
                any(seg.endswith(ext) for ext in cls.VIRTUAL_FILE_EXTENSIONS)
                for seg in path_segments
            )
            has_collection = "collections" in path_segments
            if is_gitlab_host:
                return cls._gitlab_shorthand_repo_segment_count(
                    path_segments, has_virtual_ext, has_collection
                )
            if has_virtual_ext or has_collection:
                return 2
            return len(path_segments)

        # Bare shorthand (no FQDN).  Default GitHub-style: owner/repo plus
        # any tail is treated as a virtual sub-path.  But when registry-only
        # mode is active, the proxy may be fronting a GitLab instance where
        # the project lives at an arbitrary subgroup depth -- fold non-marker
        # segments into the repo path instead of mis-classifying them as
        # virtual sub-paths (see issue: nested GitLab subgroup support).
        return cls._bare_shorthand_repo_segment_count(path_segments)

    @classmethod
    def _validate_detected_virtual_path(cls, virtual_path: str) -> None:
        """Validate a detected virtual sub-path's safety and extension shape."""
        # Security: reject path traversal in virtual path
        validate_path_segments(virtual_path, context="virtual path")

        # Reject removed `.collection.yml` extensions with a clear
        # migration message (#1094). Curated dependency aggregators
        # are now expressed as `apm.yml` with a `dependencies` block.
        if any(virtual_path.endswith(ext) for ext in cls.REMOVED_COLLECTION_EXTENSIONS):
            raise ValueError(
                f".collection.yml is no longer supported. "
                f"Convert '{virtual_path}' to an apm.yml with a "
                f"'dependencies' section. "
                f"See: https://microsoft.github.io/apm/guides/dependencies/"
            )

        # Accept any path ending in a recognised virtual file
        # extension. Reject other dotted final segments so typos like
        # `prompts/file.txt` fail fast instead of silently
        # mis-classifying as a subdirectory.
        if any(virtual_path.endswith(ext) for ext in cls.VIRTUAL_FILE_EXTENSIONS):
            return
        last_segment = virtual_path.split("/")[-1]
        if "." in last_segment:
            raise InvalidVirtualPackageExtensionError(
                f"Invalid virtual package path '{virtual_path}'. "
                f"Individual files must end with one of: {', '.join(cls.VIRTUAL_FILE_EXTENSIONS)}. "
                f"For subdirectory packages, the path should not have a file extension."
            )
