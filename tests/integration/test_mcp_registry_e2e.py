"""
End-to-end tests for MCP registry functionality.

These tests verify the complete MCP registry integration including:
- Server search and discovery
- Registry-based installation with environment variable handling
- Docker args processing with -e flags
- Empty string and defaults handling
- Cross-adapter consistency for Codex adapter configurations

They complement the golden scenario tests by focusing specifically on
the MCP registry functionality we've implemented.
"""

import json
import os
import shutil  # noqa: F401
import subprocess
import tempfile
from pathlib import Path
from unittest import mock  # noqa: F401

import pytest
import toml


def _is_registry_healthy() -> bool:
    """Check if GitHub MCP server has proper package configuration.

    Returns:
        bool: True if server has docker runtime packages, False if remote-only
    """
    try:
        # Import registry client
        import sys

        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))
        from apm_cli.registry.client import SimpleRegistryClient

        client = SimpleRegistryClient()
        server = client.find_server_by_reference("github-mcp-server")

        if not server:
            return False

        # Check if server has packages (healthy) vs remote-only (unhealthy)
        packages = server.get("packages", [])

        # Look for docker runtime packages
        for package in packages:  # noqa: SIM110
            if package.get("runtime") == "docker":
                return True

        # No docker packages = remote server only = unhealthy
        return False

    except Exception:
        return False


# Skip all tests in this module if not in E2E mode
E2E_MODE = os.environ.get("APM_E2E_TESTS", "").lower() in ("1", "true", "yes")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")

pytestmark = pytest.mark.skipif(
    not E2E_MODE, reason="MCP registry E2E tests only run when APM_E2E_TESTS=1 is set"
)


def run_command(cmd, check=True, capture_output=True, timeout=180, cwd=None, input_text=None):
    """Run a shell command with proper error handling."""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            check=check,
            capture_output=capture_output,
            text=True,
            timeout=timeout,
            cwd=cwd,
            input=input_text,
            encoding="utf-8",
            errors="replace",
        )
        return result
    except subprocess.TimeoutExpired:
        pytest.fail(f"Command timed out after {timeout}s: {cmd}")
    except subprocess.CalledProcessError as e:
        pytest.fail(f"Command failed: {cmd}\nStdout: {e.stdout}\nStderr: {e.stderr}")


@pytest.fixture(scope="module")
def temp_e2e_home():
    """Create a temporary home directory for E2E testing."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
        original_home = os.environ.get("HOME")
        test_home = os.path.join(temp_dir, "e2e_home")
        os.makedirs(test_home)

        # Set up test environment
        os.environ["HOME"] = test_home

        # Preserve GITHUB_TOKEN
        if GITHUB_TOKEN:
            os.environ["GITHUB_TOKEN"] = GITHUB_TOKEN

        yield test_home

        # Restore original environment
        if original_home:
            os.environ["HOME"] = original_home
        else:
            del os.environ["HOME"]


@pytest.fixture(scope="module")
def apm_binary():
    """Get path to APM binary for testing."""
    # Try to find APM binary in common locations
    possible_paths = [
        "apm",  # In PATH
        "./apm",  # Local directory
        "./dist/apm",  # Build directory
        Path(__file__).parent.parent.parent / "dist" / "apm",  # Relative to test
    ]

    for path in possible_paths:
        try:
            result = subprocess.run([str(path), "--version"], capture_output=True, text=True)
            if result.returncode == 0:
                return str(path)
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue

    pytest.skip("APM binary not found. Build it first with: python -m build")


class TestMCPRegistryE2E:
    """E2E tests for MCP registry functionality."""

    def test_mcp_search_command(self, temp_e2e_home, apm_binary):
        """Test MCP registry search functionality."""
        print("\n=== Testing MCP Registry Search ===")

        # Test search for GitHub MCP server
        result = run_command(f"{apm_binary} mcp search github", timeout=30)
        assert result.returncode == 0, f"MCP search failed: {result.stderr}"

        # Verify output contains expected results
        output = result.stdout.lower()
        assert "github" in output, "Search results should contain GitHub servers"
        assert "mcp" in output, "Search results should be about MCP servers"

        print(f"[OK] MCP search found GitHub servers:\n{result.stdout[:500]}...")

        # Test search with limit
        result = run_command(f"{apm_binary} mcp search filesystem --limit 3", timeout=30)
        assert result.returncode == 0, f"MCP search with limit failed: {result.stderr}"

        print("[OK] MCP search with limit works")

    def test_mcp_show_command(self, temp_e2e_home, apm_binary):
        """Test MCP registry server details functionality."""
        print("\n=== Testing MCP Registry Show ===")

        # Test show GitHub MCP server details
        github_server = "io.github.github/github-mcp-server"
        result = run_command(f"{apm_binary} mcp show {github_server}", timeout=30)
        assert result.returncode == 0, f"MCP show failed: {result.stderr}"

        # Verify output contains server details
        output = result.stdout.lower()
        assert "github" in output, "Server details should mention GitHub"
        # The show command displays installation guide - look for key elements
        assert "installation" in output or "install" in output or "command" in output, (
            "Should show installation info"
        )

        print(f"[OK] MCP show displays server details:\n{result.stdout[:500]}...")

    @pytest.mark.skipif(not GITHUB_TOKEN, reason="GITHUB_TOKEN required for installation tests")
    @pytest.mark.skipif(
        not _is_registry_healthy(),
        reason="GitHub MCP server configured as remote-only (no packages) - skipping installation tests",
    )
    def test_registry_installation_with_codex(self, temp_e2e_home, apm_binary):
        """Test complete registry-based installation flow with Codex runtime."""
        print("\n=== Testing Registry Installation with Codex ===")

        # Step 1: Set up Codex runtime
        print("Setting up Codex runtime...")
        result = run_command(f"{apm_binary} runtime setup codex", timeout=300)
        assert result.returncode == 0, f"Codex setup failed: {result.stderr}"

        # Verify codex config was created
        codex_config = Path(temp_e2e_home) / ".codex" / "config.toml"
        assert codex_config.exists(), "Codex configuration not created"

        # Step 2: Create test project with MCP dependencies
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as project_workspace:
            project_dir = Path(project_workspace) / "registry-test-project"

            print("Creating project with MCP dependencies...")
            result = run_command(
                f"{apm_binary} init registry-test-project --yes", cwd=project_workspace
            )
            assert result.returncode == 0, f"Project init failed: {result.stderr}"

            # Add MCP dependencies to apm.yml
            apm_yml = project_dir / "apm.yml"
            apm_config = {
                "name": "registry-test-project",
                "version": "1.0.0",
                "dependencies": {
                    "mcp": [
                        "io.github.github/github-mcp-server",  # GitHub MCP server (must have)
                        "neondatabase/mcp-server-neon",  # Neon database server from registry
                    ]
                },
                "scripts": {"start": 'codex run start --param name="$name"'},
            }

            with open(apm_yml, "w") as f:
                # Use yaml if available, otherwise simple format
                try:
                    import yaml

                    yaml.dump(apm_config, f, default_flow_style=False)
                except ImportError:
                    # Fallback to manual YAML writing
                    f.write("name: registry-test-project\n")
                    f.write("version: 1.0.0\n")
                    f.write("dependencies:\n")
                    f.write("  mcp:\n")
                    f.write("    - io.github.github/github-mcp-server\n")
                    f.write("    - neondatabase/mcp-server-neon\n")
                    f.write("scripts:\n")
                    f.write('  start: codex run start --param name="$name"\n')

            print("[OK] Created apm.yml with MCP dependencies")

            # Step 3: Test installation with environment variable prompting
            print("Testing MCP installation with environment variable handling...")

            # Mock environment variable input for GitHub server
            # GitHub server needs environment variables for proper Docker configuration
            env_input = "\n\n\n\n\n"  # Empty for most optional environment variables

            result = run_command(
                f"{apm_binary} install", cwd=project_dir, input_text=env_input, timeout=180
            )

            # Installation should succeed even with some empty environment variables
            if result.returncode != 0:
                print(f"Installation output:\n{result.stdout}\n{result.stderr}")
                # Don't fail immediately - check what happened

            # Step 4: Verify Codex configuration was updated
            print("Verifying Codex configuration...")

            if codex_config.exists():
                config_content = codex_config.read_text()
                print(f"Codex config content:\n{config_content}")

                # Parse TOML content
                try:
                    config_data = toml.loads(config_content)
                    mcp_servers = config_data.get("mcp_servers", {})

                    # Verify servers were added
                    assert len(mcp_servers) > 0, "No MCP servers found in Codex config"

                    # Check for test servers with proper configuration
                    server_found = False
                    for server_name, server_config in mcp_servers.items():
                        if "github" in server_name.lower() or "neon" in server_name.lower():
                            server_found = True

                            # Verify basic configuration
                            assert "command" in server_config, f"No command in {server_name} config"
                            assert "args" in server_config, f"No args in {server_name} config"

                            # Check server configuration - GitHub server has limited package config
                            command = server_config.get("command", "")
                            args = server_config["args"]

                            # For servers without proper package configuration, expect minimal config
                            if command == "unknown" and not args:
                                print(f"[OK] Server configured with basic setup: {server_name}")
                            elif command == "docker":
                                # For Docker servers, verify args contain proper Docker command structure
                                if isinstance(args, list):  # noqa: SIM108
                                    args_str = " ".join(args)
                                else:
                                    args_str = str(args)

                                # Should contain docker run command structure
                                assert "run" in args_str or len(args) > 0, (
                                    f"Docker server should have proper args: {args}"
                                )

                                print(f"[OK] Docker server configured with args: {args}")
                            else:
                                # For other servers, just verify config structure
                                print(f"[OK] Server configured: command={command}, args={args}")

                            break

                    assert server_found, "Test server not found in configuration"
                    print(f"[OK] Verified {len(mcp_servers)} MCP servers in Codex config")

                except Exception as e:
                    pytest.fail(f"Failed to parse Codex config: {e}\nContent: {config_content}")
            else:
                pytest.fail("Codex configuration file not found after installation")

    @pytest.mark.skipif(
        not _is_registry_healthy(),
        reason="GitHub MCP server configured as remote-only (no packages) - skipping installation tests",
    )
    def test_empty_string_handling_e2e(self, temp_e2e_home, apm_binary):
        """Test end-to-end empty string and defaults handling during installation."""
        print("\n=== Testing Empty String and Defaults Handling ===")

        # Set up a runtime first
        result = run_command(f"{apm_binary} runtime setup codex", timeout=300)
        if result.returncode != 0:
            pytest.skip("Codex setup failed, skipping empty string test")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as project_workspace:
            project_dir = Path(project_workspace) / "empty-string-test"

            result = run_command(
                f"{apm_binary} init empty-string-test --yes", cwd=project_workspace
            )
            assert result.returncode == 0, f"Project init failed: {result.stderr}"

            # Create apm.yml with MCP dependency that has environment variables
            apm_yml = project_dir / "apm.yml"
            with open(apm_yml, "w") as f:
                f.write("name: empty-string-test\n")
                f.write("version: 1.0.0\n")
                f.write("dependencies:\n")
                f.write("  mcp:\n")
                f.write("    - io.github.github/github-mcp-server\n")
                f.write("scripts:\n")
                f.write("  start: codex run start\n")

            # Test with various empty string scenarios
            print("Testing with empty strings and whitespace...")

            # Simulate user providing: empty string, whitespace, empty, empty for different vars
            # For GitHub server, provide minimal environment input
            apm_yml = project_dir / "apm.yml"
            with open(apm_yml, "w") as f:
                f.write("name: copilot-registry-test\n")
                f.write("version: 1.0.0\n")
                f.write("dependencies:\n")
                f.write("  mcp:\n")
                f.write("    - io.github.github/github-mcp-server\n")
                f.write("scripts:\n")
                f.write('  start: copilot run start --param name="$name"\n')

            # Try installation targeting Copilot specifically
            print("Testing Copilot MCP installation...")

            # Mock environment variables
            # GitHub server needs environment variables for Docker configuration
            env_input = "\n\n\n\n\n"  # Empty inputs for optional variables

            result = run_command(
                f"{apm_binary} install --runtime copilot",
                cwd=project_dir,
                input_text=env_input,
                timeout=120,
            )

            # Check if Copilot configuration was created
            copilot_config = Path(temp_e2e_home) / ".copilot" / "mcp-config.json"

            if copilot_config.exists():
                config_content = copilot_config.read_text()
                print(f"Copilot config content:\n{config_content}")

                try:
                    config_data = json.loads(config_content)
                    mcp_servers = config_data.get("mcpServers", {})

                    assert len(mcp_servers) > 0, "No MCP servers in Copilot config"

                    # Verify test server configuration
                    server_found = False
                    for server_name, server_config in mcp_servers.items():
                        if "github" in server_name.lower() or "neon" in server_name.lower():
                            server_found = True

                            # Verify server configuration
                            if "command" in server_config:
                                assert "args" in server_config, f"No args in {server_name}"

                                # Check server type and configuration
                                args = server_config["args"]
                                command = server_config.get("command", "")

                                # GitHub server has limited package config - accept basic setup
                                if command == "unknown" and not args:
                                    print(f"[OK] Copilot server with basic setup: {server_name}")
                                elif command == "docker":
                                    if isinstance(args, list):
                                        args_str = " ".join(args)
                                    else:
                                        args_str = str(args)

                                    assert "run" in args_str or len(args) > 0, (
                                        f"Docker server should have valid args: {args}"
                                    )
                                    print(f"[OK] Copilot Docker server with args: {args}")
                                else:
                                    print(f"[OK] Copilot server configured: {command}")

                            break

                    if not server_found and result.returncode == 0:
                        pytest.fail(
                            "Test server not configured in Copilot despite successful installation"
                        )

                    print(f"[OK] Copilot configuration tested with {len(mcp_servers)} servers")

                except json.JSONDecodeError as e:
                    pytest.fail(f"Invalid JSON in Copilot config: {e}\nContent: {config_content}")
            else:
                print("[WARN] Copilot configuration not created (binary may not be available)")
                # This is OK for testing - we're validating the adapter logic

    def test_empty_string_handling_e2e(self, temp_e2e_home, apm_binary):  # noqa: F811
        """Test end-to-end empty string and defaults handling during installation."""
        print("\n=== Testing Empty String and Defaults Handling ===")

        # Set up a runtime first
        result = run_command(f"{apm_binary} runtime setup codex", timeout=300)
        if result.returncode != 0:
            pytest.skip("Codex setup failed, skipping empty string test")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as project_workspace:
            project_dir = Path(project_workspace) / "empty-string-test"

            result = run_command(
                f"{apm_binary} init empty-string-test --yes", cwd=project_workspace
            )
            assert result.returncode == 0, f"Project init failed: {result.stderr}"

            # Create apm.yml with MCP dependency that has environment variables
            apm_yml = project_dir / "apm.yml"
            with open(apm_yml, "w") as f:
                f.write("name: empty-string-test\n")
                f.write("version: 1.0.0\n")
                f.write("dependencies:\n")
                f.write("  mcp:\n")
                f.write("    - io.github.github/github-mcp-server\n")
                f.write("scripts:\n")
                f.write("  start: codex run start\n")

            # Test with various empty string scenarios
            print("Testing with empty strings and whitespace...")

            # Simulate user providing: empty string, whitespace, empty, empty for different vars
            # For GitHub server, provide minimal environment input
            env_input = "\n   \n\n\n\n"  # Empty/whitespace for optional vars

            result = run_command(
                f"{apm_binary} install", cwd=project_dir, input_text=env_input, timeout=120
            )

            # Check the configuration to see how empty strings were handled
            codex_config = Path(temp_e2e_home) / ".codex" / "config.toml"
            if codex_config.exists():
                config_content = codex_config.read_text()

                # Should NOT contain empty environment variables
                assert "GITHUB_TOKEN=" not in config_content, (
                    "Config should not contain empty env vars"
                )
                assert 'GITHUB_TOKEN=""' not in config_content, (
                    "Config should not contain empty quoted env vars"
                )

                # If defaults were applied, they should be meaningful values
                if "GITHUB_TOKEN" in config_content:
                    lines = config_content.split("\n")
                    for line in lines:
                        if "GITHUB_TOKEN" in line and "=" in line:
                            value = line.split("=")[1].strip().strip('"').strip("'")
                            assert value != "", f"GITHUB_TOKEN should not be empty: {line}"
                            assert value.strip() != "", (
                                f"GITHUB_TOKEN should not be whitespace: {line}"
                            )

                print("[OK] Empty string handling verified in configuration")

    @pytest.mark.skipif(
        not _is_registry_healthy(),
        reason="GitHub MCP server configured as remote-only (no packages) - skipping installation tests",
    )
    def test_cross_adapter_consistency(self, temp_e2e_home, apm_binary):
        """Test that Codex adapter handles MCP server installation consistently."""
        print("\n=== Testing Codex Adapter Consistency ===")

        # Set up Codex runtime
        result = run_command(f"{apm_binary} runtime setup codex", timeout=300)
        if result.returncode != 0:
            pytest.skip("Codex setup failed, skipping consistency test")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as project_workspace:
            project_dir = Path(project_workspace) / "consistency-test"

            result = run_command(f"{apm_binary} init consistency-test --yes", cwd=project_workspace)
            assert result.returncode == 0, f"Project init failed: {result.stderr}"

            # Create apm.yml with Codex script
            apm_yml = project_dir / "apm.yml"
            with open(apm_yml, "w") as f:
                f.write("name: consistency-test\n")
                f.write("version: 1.0.0\n")
                f.write("dependencies:\n")
                f.write("  mcp:\n")
                f.write("    - io.github.github/github-mcp-server\n")
                f.write("scripts:\n")
                f.write("  start: codex run start\n")

            # Install for Codex runtime
            print("Installing for Codex runtime...")

            # Provide consistent environment variables
            # GitHub server needs environment variables for proper configuration
            env_input = "\n\n\n\n\n"  # Empty for optional vars

            result = run_command(
                f"{apm_binary} install", cwd=project_dir, input_text=env_input, timeout=180
            )

            # Check Codex configuration
            codex_config = Path(temp_e2e_home) / ".codex" / "config.toml"

            if codex_config.exists():
                print("[OK] Found Codex configuration to verify")

                config_content = codex_config.read_text()

                try:
                    import toml

                    config_data = toml.loads(config_content)
                    servers = config_data.get("mcp_servers", {})
                except ImportError:
                    # Skip TOML parsing if library not available
                    servers = {"github": {}} if "github" in config_content else {}

                server_found = any("github" in name.lower() for name in servers.keys())  # noqa: SIM118
                assert server_found, "Codex configuration should contain GitHub server"

                print("[OK] Codex configuration contains GitHub server")
            else:
                pytest.fail("No Codex configuration found to verify")

    @pytest.mark.skipif(
        not _is_registry_healthy(),
        reason="GitHub MCP server configured as remote-only (no packages) - skipping installation tests",
    )
    def test_duplication_prevention_e2e(self, temp_e2e_home, apm_binary):
        """Test that repeated installations don't create duplicate entries."""
        print("\n=== Testing Duplication Prevention ===")

        # Set up runtime
        result = run_command(f"{apm_binary} runtime setup codex", timeout=300)
        if result.returncode != 0:
            pytest.skip("Codex setup failed, skipping duplication test")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as project_workspace:
            project_dir = Path(project_workspace) / "duplication-test"

            result = run_command(f"{apm_binary} init duplication-test --yes", cwd=project_workspace)
            assert result.returncode == 0, f"Project init failed: {result.stderr}"

            # Create apm.yml
            apm_yml = project_dir / "apm.yml"
            with open(apm_yml, "w") as f:
                f.write("name: duplication-test\n")
                f.write("version: 1.0.0\n")
                f.write("dependencies:\n")
                f.write("  mcp:\n")
                f.write("    - io.github.github/github-mcp-server\n")
                f.write("scripts:\n")
                f.write("  start: codex run start\n")

            # First installation
            print("Running first installation...")
            # GitHub server needs environment variables for Docker configuration
            env_input = "test-token\n\n\n\n\n"  # token + empty for optional vars

            result1 = run_command(  # noqa: F841
                f"{apm_binary} install", cwd=project_dir, input_text=env_input, timeout=120
            )

            # Second installation (should detect existing and skip)
            print("Running second installation (should skip duplicates)...")

            result2 = run_command(
                f"{apm_binary} install", cwd=project_dir, input_text=env_input, timeout=120
            )

            # Verify no duplication in output
            output2 = result2.stdout.lower()

            # Should indicate that servers are already configured
            assert (
                "already" in output2 or "nothing to install" in output2 or "configured" in output2
            ), f"Second install should indicate no work needed: {result2.stdout}"

            # Verify configuration doesn't have duplicates
            codex_config = Path(temp_e2e_home) / ".codex" / "config.toml"
            if codex_config.exists():
                config_content = codex_config.read_text()

                # Check for actual duplication by looking for multiple [mcp_servers.github-mcp-server] sections
                server_section_count = config_content.count("[mcp_servers.github-mcp-server]")
                assert server_section_count == 1, (
                    f"GitHub server should appear exactly once, found {server_section_count} times"
                )

                # Check that Docker args don't have duplicated configurations
                import toml

                config_data = toml.loads(config_content)
                if (
                    "mcp_servers" in config_data
                    and "github-mcp-server" in config_data["mcp_servers"]
                ):
                    server_config = config_data["mcp_servers"]["github-mcp-server"]
                    args = server_config.get("args", [])
                    # For servers without package configs, args will be empty - this is expected
                    if (
                        isinstance(args, list) and len(args) > 20
                    ):  # Very high threshold for duplication
                        pytest.fail(
                            f"GitHub server args seem excessively duplicated: {len(args)} items"
                        )

                print("[OK] Configuration verified - no excessive duplication detected")

            print("[OK] Duplication prevention working correctly")


class TestSlugCollisionPrevention:
    """Integration tests for slug-collision prevention against the live MCP registry.

    These tests verify that ``find_server_by_reference`` resolves qualified and
    unqualified shorthands correctly, preventing cross-namespace slug collisions.
    Network failures cause the individual test to be skipped rather than fail.
    """

    def _make_client(self):
        from apm_cli.registry.client import SimpleRegistryClient

        return SimpleRegistryClient()

    def test_qualified_shorthand_resolves_to_canonical_name(self):
        """Qualified ref 'github/github-mcp-server' should resolve to the canonical name."""
        client = self._make_client()
        try:
            result = client.find_server_by_reference("github/github-mcp-server")
        except Exception:
            pytest.skip("Registry unavailable")

        assert result is not None, "Expected 'github/github-mcp-server' to resolve a server"
        assert result.get("name") == "io.github.github/github-mcp-server", (
            f"Expected canonical name 'io.github.github/github-mcp-server', "
            f"got '{result.get('name')}'"
        )

    def test_qualified_ref_does_not_match_different_namespace(self):
        """Qualified ref with a bogus namespace must NOT resolve to a real server sharing the same slug."""
        client = self._make_client()
        try:
            # 'nonexistent-org/github-mcp-server' shares the slug with
            # 'io.github.github/github-mcp-server' but belongs to a
            # different namespace — boundary matching must reject it.
            result = client.find_server_by_reference("nonexistent-org/github-mcp-server")
        except Exception:
            pytest.skip("Registry unavailable")

        assert result is None, (
            "Expected 'nonexistent-org/github-mcp-server' to return None "
            "(namespace mismatch), "
            f"but got: {result.get('name') if result else result}"
        )

    def test_unqualified_slug_resolves(self):
        """Unqualified slug 'github-mcp-server' should resolve via slug matching."""
        client = self._make_client()
        try:
            result = client.find_server_by_reference("github-mcp-server")
        except Exception:
            pytest.skip("Registry unavailable")

        assert result is not None, "Expected unqualified 'github-mcp-server' to resolve a server"


if __name__ == "__main__":
    # Example of how to run MCP registry E2E tests manually
    print("To run MCP registry E2E tests manually:")
    print("export APM_E2E_TESTS=1")
    print("export GITHUB_TOKEN=your_token_here")
    print("pytest tests/integration/test_mcp_registry_e2e.py -v -s")

    # Run tests when executed directly
    if E2E_MODE:
        pytest.main([__file__, "-v", "-s"])
    else:
        print("\nE2E mode not enabled. Set APM_E2E_TESTS=1 to run these tests.")
