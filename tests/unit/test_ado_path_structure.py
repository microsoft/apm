"""Unit tests for Azure DevOps (ADO) path structure handling.

Tests that:
1. GitHub dependencies use 2-part paths: apm_modules/owner/repo
2. ADO dependencies use 3-part paths: apm_modules/org/project/repo
3. Primitive discovery works correctly with ADO 3-level structure
4. Compilation finds and processes ADO dependency primitives
"""

from pathlib import Path

import pytest

from apm_cli.models.apm_package import DependencyReference

pytestmark = pytest.mark.unit


class TestADOPathStructure:
    """Test ADO vs GitHub path structure handling."""

    def test_github_dependency_uses_2_part_path(self):
        """Test that GitHub dependencies use owner/repo (2-part) structure."""
        dep = DependencyReference.parse("microsoft/apm-sample-package")

        assert dep.is_azure_devops() is False
        assert dep.repo_url == "microsoft/apm-sample-package"

        # Path parts for installation
        parts = dep.repo_url.split("/")
        assert len(parts) == 2
        assert parts[0] == "microsoft"
        assert parts[1] == "apm-sample-package"

    def test_ado_dependency_uses_3_part_path(self):
        """Test that ADO dependencies use org/project/repo (3-part) structure."""
        dep = DependencyReference.parse(
            "dev.azure.com/dmeppiel-org/market-js-app/_git/compliance-rules"
        )

        assert dep.is_azure_devops() is True
        assert dep.repo_url == "dmeppiel-org/market-js-app/compliance-rules"
        assert dep.ado_organization == "dmeppiel-org"
        assert dep.ado_project == "market-js-app"
        assert dep.ado_repo == "compliance-rules"

        # Path parts for installation
        parts = dep.repo_url.split("/")
        assert len(parts) == 3
        assert parts[0] == "dmeppiel-org"
        assert parts[1] == "market-js-app"
        assert parts[2] == "compliance-rules"

    def test_ado_simplified_format_uses_3_part_path(self):
        """Test that simplified ADO format also produces 3-part path."""
        dep = DependencyReference.parse("dev.azure.com/myorg/myproject/myrepo")

        assert dep.is_azure_devops() is True
        assert dep.repo_url == "myorg/myproject/myrepo"

        parts = dep.repo_url.split("/")
        assert len(parts) == 3


class TestInstallPathLogic:
    """Test the install path logic differentiates GitHub and ADO correctly."""

    def test_github_install_path_is_2_levels(self):
        """Verify GitHub dependencies would install to 2-level path."""
        dep = DependencyReference.parse("owner/repo")

        # Simulate install path logic from cli.py
        repo_parts = dep.repo_url.split("/")

        if dep.is_azure_devops() and len(repo_parts) >= 3:
            install_parts = [repo_parts[0], repo_parts[1], repo_parts[2]]
        elif len(repo_parts) >= 2:
            install_parts = [repo_parts[0], repo_parts[1]]
        else:
            install_parts = [dep.repo_url]

        # Should be 2 levels for GitHub
        assert len(install_parts) == 2
        assert install_parts == ["owner", "repo"]

    def test_ado_install_path_is_3_levels(self):
        """Verify ADO dependencies would install to 3-level path."""
        dep = DependencyReference.parse("dev.azure.com/org/project/_git/repo")

        # Simulate install path logic from cli.py
        repo_parts = dep.repo_url.split("/")

        if dep.is_azure_devops() and len(repo_parts) >= 3:
            install_parts = [repo_parts[0], repo_parts[1], repo_parts[2]]
        elif len(repo_parts) >= 2:
            install_parts = [repo_parts[0], repo_parts[1]]
        else:
            install_parts = [dep.repo_url]

        # Should be 3 levels for ADO
        assert len(install_parts) == 3
        assert install_parts == ["org", "project", "repo"]

    def test_discovery_path_matches_install_path_for_github(self):
        """Verify discovery path logic matches install path logic for GitHub."""
        dep_name = "owner/repo"  # As returned by get_dependency_declaration_order

        # Simulate discovery path logic from discovery.py
        parts = dep_name.split("/")
        if len(parts) >= 3:
            discovery_path = f"{parts[0]}/{parts[1]}/{parts[2]}"
        elif len(parts) == 2:
            discovery_path = f"{parts[0]}/{parts[1]}"
        else:
            discovery_path = dep_name

        # Simulate install path logic
        dep = DependencyReference.parse(dep_name)
        repo_parts = dep.repo_url.split("/")
        if dep.is_azure_devops() and len(repo_parts) >= 3:
            install_path = f"{repo_parts[0]}/{repo_parts[1]}/{repo_parts[2]}"
        elif len(repo_parts) >= 2:
            install_path = f"{repo_parts[0]}/{repo_parts[1]}"
        else:
            install_path = dep.repo_url

        # Paths should match
        assert discovery_path == install_path

    def test_discovery_path_matches_install_path_for_ado(self):
        """Verify discovery path logic matches install path logic for ADO."""
        # The dep_name in apm.yml would be the full ADO URL
        # get_dependency_declaration_order extracts repo_url = "org/project/repo"
        dep_name = "org/project/repo"  # As returned by get_dependency_declaration_order

        # Simulate discovery path logic from discovery.py
        parts = dep_name.split("/")
        if len(parts) >= 3:
            discovery_path = f"{parts[0]}/{parts[1]}/{parts[2]}"
        elif len(parts) == 2:
            discovery_path = f"{parts[0]}/{parts[1]}"
        else:
            discovery_path = dep_name

        # Simulate install path logic for ADO
        dep = DependencyReference.parse("dev.azure.com/org/project/_git/repo")
        repo_parts = dep.repo_url.split("/")
        if dep.is_azure_devops() and len(repo_parts) >= 3:
            install_path = f"{repo_parts[0]}/{repo_parts[1]}/{repo_parts[2]}"
        elif len(repo_parts) >= 2:
            install_path = f"{repo_parts[0]}/{repo_parts[1]}"
        else:
            install_path = dep.repo_url

        # Paths should match
        assert discovery_path == install_path
        assert discovery_path == "org/project/repo"


class TestADOVirtualPackagePaths:
    """Test that ADO virtual packages (collections and individual files) use correct paths."""

    def test_github_virtual_package_uses_2_level_path(self):
        """Verify GitHub virtual packages install to 2-level path."""
        dep = DependencyReference.parse("owner/test-repo/collections/project-planning")

        assert dep.is_virtual is True
        assert dep.is_virtual_subdirectory() is True

        # Simulate install path logic from cli.py for virtual packages
        repo_parts = dep.repo_url.split("/")
        virtual_name = dep.get_virtual_package_name()

        if dep.is_azure_devops() and len(repo_parts) >= 3:
            install_path = f"{repo_parts[0]}/{repo_parts[1]}/{virtual_name}"
        elif len(repo_parts) >= 2:
            install_path = f"{repo_parts[0]}/{virtual_name}"
        else:
            install_path = virtual_name

        # Should be 2 levels for GitHub: owner/virtual-pkg-name
        assert install_path == "owner/test-repo-project-planning"

    def test_ado_virtual_collection_uses_3_level_path(self):
        """Verify ADO virtual collections install to 3-level path."""
        dep = DependencyReference.parse(
            "dev.azure.com/myorg/myproject/myrepo/collections/my-collection"
        )

        assert dep.is_azure_devops() is True
        assert dep.is_virtual is True
        assert dep.is_virtual_subdirectory() is True
        assert dep.repo_url == "myorg/myproject/myrepo"

        # Simulate install path logic from cli.py for virtual packages
        repo_parts = dep.repo_url.split("/")
        virtual_name = dep.get_virtual_package_name()

        if dep.is_azure_devops() and len(repo_parts) >= 3:
            install_path = f"{repo_parts[0]}/{repo_parts[1]}/{virtual_name}"
        elif len(repo_parts) >= 2:
            install_path = f"{repo_parts[0]}/{virtual_name}"
        else:
            install_path = virtual_name

        # Should be 3 levels for ADO: org/project/virtual-pkg-name
        assert install_path == "myorg/myproject/myrepo-my-collection"

    def test_ado_virtual_file_uses_3_level_path(self):
        """Verify ADO virtual files install to 3-level path."""
        dep = DependencyReference.parse(
            "dev.azure.com/myorg/myproject/myrepo/prompts/code-review.prompt.md"
        )

        assert dep.is_azure_devops() is True
        assert dep.is_virtual is True
        assert dep.is_virtual_file() is True
        assert dep.repo_url == "myorg/myproject/myrepo"

        # Simulate install path logic from cli.py for virtual packages
        repo_parts = dep.repo_url.split("/")
        virtual_name = dep.get_virtual_package_name()

        if dep.is_azure_devops() and len(repo_parts) >= 3:
            install_path = f"{repo_parts[0]}/{repo_parts[1]}/{virtual_name}"
        elif len(repo_parts) >= 2:
            install_path = f"{repo_parts[0]}/{virtual_name}"
        else:
            install_path = virtual_name

        # Should be 3 levels for ADO: org/project/virtual-pkg-name
        assert install_path == "myorg/myproject/myrepo-code-review"

    def test_ado_collection_with_git_segment(self):
        """Verify ADO collections with _git segment work correctly."""
        dep = DependencyReference.parse(
            "dev.azure.com/myorg/myproject/_git/copilot-instructions/collections/csharp-ddd"
        )

        assert dep.is_azure_devops() is True
        assert dep.is_virtual is True
        assert dep.is_virtual_subdirectory() is True
        assert dep.repo_url == "myorg/myproject/copilot-instructions"
        assert dep.virtual_path == "collections/csharp-ddd"

        # Verify correct install path
        repo_parts = dep.repo_url.split("/")
        virtual_name = dep.get_virtual_package_name()

        assert len(repo_parts) == 3
        assert virtual_name == "copilot-instructions-csharp-ddd"

        # Install path should be org/project/virtual-pkg-name
        install_path = f"{repo_parts[0]}/{repo_parts[1]}/{virtual_name}"
        assert install_path == "myorg/myproject/copilot-instructions-csharp-ddd"

    def test_ado_collection_missing_repo_name_parsed_incorrectly(self):
        """Document the behavior when repo name is omitted from ADO collection path.

        This test documents that if a user omits the repo name, 'collections' is
        incorrectly parsed as the repo name. Users must include the full path:
        org/project/repo/collections/name
        """
        # User mistake: omitting repo name
        dep = DependencyReference.parse("dev.azure.com/myorg/myproject/collections/my-collection")

        # 'collections' is incorrectly parsed as repo name
        assert dep.repo_url == "myorg/myproject/collections"
        assert dep.virtual_path == "my-collection"

        # The shape no longer matches the SUBDIRECTORY heuristic
        # (virtual_path is `my-collection`, no extension) so it is treated
        # as a subdirectory virtual ref under repo `myorg/myproject/collections`
        # -- the install will fail downstream with a 'repo not found' error.
        assert dep.is_virtual_subdirectory() is True
        assert dep.is_virtual_file() is False


class TestADOPruneCommand:
    """Test that prune command handles ADO 3-level paths correctly."""

    def test_prune_path_parts_extraction(self):
        """Verify path parts are correctly extracted for both GitHub and ADO."""
        # GitHub 2-level path
        github_path = "owner/repo"
        github_parts = github_path.split("/")
        assert len(github_parts) == 2
        assert github_parts[0] == "owner"

        # ADO 3-level path
        ado_path = "org/project/repo"
        ado_parts = ado_path.split("/")
        assert len(ado_parts) == 3
        assert ado_parts[0] == "org"
        assert ado_parts[1] == "project"
        assert ado_parts[2] == "repo"

    def test_prune_cleanup_uses_path_parts_not_org_name(self):
        """Verify prune uses path_parts[0] for org directory cleanup (not undefined org_name)."""
        # This test ensures the fix for the undefined org_name bug
        org_repo_name = "org/project/repo"
        path_parts = org_repo_name.split("/")

        # Must use path_parts[0], not org_name (which was undefined)
        org_path_component = path_parts[0]
        assert org_path_component == "org"

        # For ADO 3-level, project directory should also be cleaned
        if len(path_parts) >= 3:
            project_path_components = path_parts[0], path_parts[1]
            assert project_path_components == ("org", "project")

    def test_prune_joinpath_works_for_variable_depth(self):
        """Verify joinpath(*path_parts) works for both 2-level and 3-level paths."""

        base = Path("/tmp/apm_modules")

        # GitHub 2-level
        github_parts = ["owner", "repo"]
        github_path = base.joinpath(*github_parts)
        assert github_path.as_posix().endswith("/tmp/apm_modules/owner/repo")

        # ADO 3-level
        ado_parts = ["org", "project", "repo"]
        ado_path = base.joinpath(*ado_parts)
        assert ado_path.as_posix().endswith("/tmp/apm_modules/org/project/repo")


class TestADOFullURLSubPath:
    """Test 8-shape matrix for ADO URL parsing (issue #1128)."""

    @pytest.mark.parametrize(
        "input_str,expected_virtual,expected_virtual_path,expected_ref,expected_host,expected_org,expected_project,expected_repo",
        [
            # Case 1: shorthand base
            (
                "dev.azure.com/org/proj/_git/repo",
                False,
                None,
                None,
                "dev.azure.com",
                "org",
                "proj",
                "repo",
            ),
            # Case 2: shorthand + virtual
            (
                "dev.azure.com/org/proj/_git/repo/sub/path",
                True,
                "sub/path",
                None,
                "dev.azure.com",
                "org",
                "proj",
                "repo",
            ),
            # Case 3: shorthand + ref
            (
                "dev.azure.com/org/proj/_git/repo#main",
                False,
                None,
                "main",
                "dev.azure.com",
                "org",
                "proj",
                "repo",
            ),
            # Case 4: shorthand + virtual + ref
            (
                "dev.azure.com/org/proj/_git/repo/sub/path#main",
                True,
                "sub/path",
                "main",
                "dev.azure.com",
                "org",
                "proj",
                "repo",
            ),
            # Case 5: full URL base
            (
                "https://dev.azure.com/org/proj/_git/repo",
                False,
                None,
                None,
                "dev.azure.com",
                "org",
                "proj",
                "repo",
            ),
            # Case 6: full URL + ref
            (
                "https://dev.azure.com/org/proj/_git/repo#main",
                False,
                None,
                "main",
                "dev.azure.com",
                "org",
                "proj",
                "repo",
            ),
            # Case 7: full URL + virtual (THE BUG)
            (
                "https://dev.azure.com/org/proj/_git/repo/sub/path",
                True,
                "sub/path",
                None,
                "dev.azure.com",
                "org",
                "proj",
                "repo",
            ),
            # Case 8: full URL + virtual + ref (THE BUG)
            (
                "https://dev.azure.com/org/proj/_git/repo/sub/path#main",
                True,
                "sub/path",
                "main",
                "dev.azure.com",
                "org",
                "proj",
                "repo",
            ),
            # Case 9: VS shorthand base
            (
                "myorg.visualstudio.com/proj/_git/repo",
                False,
                None,
                None,
                "myorg.visualstudio.com",
                "myorg",
                "proj",
                "repo",
            ),
            # Case 10: VS shorthand + virtual
            (
                "myorg.visualstudio.com/proj/_git/repo/sub/path",
                True,
                "sub/path",
                None,
                "myorg.visualstudio.com",
                "myorg",
                "proj",
                "repo",
            ),
            # Case 11: VS shorthand + ref
            (
                "myorg.visualstudio.com/proj/_git/repo#main",
                False,
                None,
                "main",
                "myorg.visualstudio.com",
                "myorg",
                "proj",
                "repo",
            ),
            # Case 12: VS shorthand + virtual + ref
            (
                "myorg.visualstudio.com/proj/_git/repo/sub/path#main",
                True,
                "sub/path",
                "main",
                "myorg.visualstudio.com",
                "myorg",
                "proj",
                "repo",
            ),
            # Case 13: VS full URL base
            (
                "https://myorg.visualstudio.com/proj/_git/repo",
                False,
                None,
                None,
                "myorg.visualstudio.com",
                "myorg",
                "proj",
                "repo",
            ),
            # Case 14: VS full URL + ref
            (
                "https://myorg.visualstudio.com/proj/_git/repo#main",
                False,
                None,
                "main",
                "myorg.visualstudio.com",
                "myorg",
                "proj",
                "repo",
            ),
            # Case 15: VS full URL + virtual
            (
                "https://myorg.visualstudio.com/proj/_git/repo/sub/path",
                True,
                "sub/path",
                None,
                "myorg.visualstudio.com",
                "myorg",
                "proj",
                "repo",
            ),
            # Case 16: VS full URL + virtual + ref
            (
                "https://myorg.visualstudio.com/proj/_git/repo/sub/path#main",
                True,
                "sub/path",
                "main",
                "myorg.visualstudio.com",
                "myorg",
                "proj",
                "repo",
            ),
        ],
    )
    def test_ado_url_matrix(
        self,
        input_str: str,
        expected_virtual: bool,
        expected_virtual_path: str | None,
        expected_ref: str | None,
        expected_host: str,
        expected_org: str,
        expected_project: str,
        expected_repo: str,
    ) -> None:
        """All ADO URL forms parse correctly (issue #1128)."""
        dep = DependencyReference.parse(input_str)

        assert dep.host == expected_host
        assert dep.ado_organization == expected_org
        assert dep.ado_project == expected_project
        assert dep.ado_repo == expected_repo
        assert dep.is_virtual == expected_virtual
        assert dep.virtual_path == expected_virtual_path
        assert dep.reference == expected_ref
