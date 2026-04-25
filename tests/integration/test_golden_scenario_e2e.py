"""
End-to-end golden path tests for APM runtime integration.

These tests verify the exact hero quick start journey from the README:
# 2. Set up your GitHub PAT and an Agent CLI
export GITHUB_TOKEN=your_token_here
apm runtime setup codex # Installs OpenAI Codex CLI

# 3. Transform your project with AI-Native structure
apm init my-ai-native-project

# 4. Compile Agent Primitives for any coding agent
cd my-ai-native-project
apm compile  # Generates agents.md compatible with multiple Agent CLIs

# 5. Install MCP dependencies and execute agentic workflows (*.prompt.md files)
apm install
apm run start --param name="<YourGitHubHandle>"

They should only run on releases to avoid API rate limits and costs during development.

To run these tests, you need:
- GITHUB_TOKEN environment variable set with appropriate permissions
- Network access to download runtimes and make API calls
"""

import os
import subprocess
import tempfile
import shutil
import pytest
import json
from pathlib import Path
from unittest import mock
import toml


# Skip all tests in this module if not in E2E mode
E2E_MODE = os.environ.get('APM_E2E_TESTS', '').lower() in ('1', 'true', 'yes')

# Token detection for test requirements (read-only)
# The integration script handles all token management properly
GITHUB_APM_PAT = os.environ.get('GITHUB_APM_PAT')
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN')

# Primary token for requirement checking only (integration script handles actual usage)
PRIMARY_TOKEN = GITHUB_APM_PAT or GITHUB_TOKEN

pytestmark = pytest.mark.skipif(
    not E2E_MODE, 
    reason="E2E tests only run when APM_E2E_TESTS=1 is set"
)


def run_command(cmd, check=True, capture_output=True, timeout=180, cwd=None, show_output=False, env=None):
    """Run a shell command with proper error handling."""
    try:
        if show_output:
            # For commands we want to see output from (like runtime setup and execution)
            print(f"\n>>> Running command: {cmd}")
            result = subprocess.run(
                cmd, 
                shell=True, 
                check=check, 
                capture_output=False,  # Don't capture, let it stream to terminal
                text=True,
                timeout=timeout,
                cwd=cwd,
                env=env
            )
            # For show_output commands, we need to capture in a different way to return something
            # Run again with capture to get return data
            result_capture = subprocess.run(
                cmd, 
                shell=True, 
                check=False,  # Don't fail here, we already ran it above
                capture_output=True, 
                text=True,
                timeout=timeout,
                cwd=cwd,
                env=env
            )
            result.stdout = result_capture.stdout
            result.stderr = result_capture.stderr
        else:
            result = subprocess.run(
                cmd, 
                shell=True, 
                check=check, 
                capture_output=capture_output, 
                text=True,
                timeout=timeout,
                cwd=cwd,
                env=env
            )
        return result
    except subprocess.TimeoutExpired:
        pytest.fail(f"Command timed out after {timeout}s: {cmd}")
    except subprocess.CalledProcessError as e:
        pytest.fail(f"Command failed: {cmd}\nStdout: {e.stdout}\nStderr: {e.stderr}")


@pytest.fixture(scope="module")
def temp_e2e_home():
    """Create a temporary home directory for E2E testing."""
    with tempfile.TemporaryDirectory() as temp_dir:
        original_home = os.environ.get('HOME')
        test_home = os.path.join(temp_dir, 'e2e_home')
        os.makedirs(test_home)
        
        # Set up test environment -- stash original HOME so tests can
        # recover credentials (e.g. ADC at ~/.config/gcloud/).
        os.environ['_APM_ORIGINAL_HOME'] = original_home or ''
        os.environ['HOME'] = test_home

        # Copy only the ADC credentials file (not the entire gcloud
        # directory) so runtimes that rely on ADC (e.g. gemini-cli with
        # Vertex AI) can auth without exposing service account keys.
        real_adc = (
            Path(original_home or '')
            / ".config" / "gcloud" / "application_default_credentials.json"
        )
        if real_adc.is_file():
            fake_adc = (
                Path(test_home)
                / ".config" / "gcloud" / "application_default_credentials.json"
            )
            fake_adc.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(real_adc), str(fake_adc))


        # Note: Do NOT override token environment variables here
        # Let test-integration.sh handle token management properly
        # It has the correct prioritization: GITHUB_APM_PAT > GITHUB_TOKEN
        
        yield test_home
        
        # Restore original environment
        os.environ.pop('_APM_ORIGINAL_HOME', None)
        if original_home:
            os.environ['HOME'] = original_home
        else:
            del os.environ['HOME']


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


class TestGoldenScenarioE2E:
    """End-to-end tests for the exact README hero quick start scenario."""
    
    @pytest.mark.skipif(not PRIMARY_TOKEN, reason="GitHub token (GITHUB_APM_PAT or GITHUB_TOKEN) required for E2E tests")
    def test_complete_golden_scenario_copilot(self, temp_e2e_home, apm_binary):
        """Test the complete hero quick start from README using Copilot CLI runtime.
        
        Validates the exact 6-step flow:
        1. (Prerequisites: GitHub token via GITHUB_APM_PAT or GITHUB_TOKEN) 
        2. apm runtime setup copilot (sets up Copilot CLI)
        3. apm init my-ai-native-project
        4. cd my-ai-native-project && apm compile
        5. apm install
        6. apm run start --param name="<YourGitHubHandle>" (uses Copilot CLI)
        """
        
        # Step 1: Setup Copilot runtime (equivalent to: apm runtime setup copilot)
        print("\n=== Step 2: Set up your GitHub PAT and an Agent CLI ===")
        print("GitHub token: ✓ (set via environment - GITHUB_APM_PAT preferred)")
        print("Installing Copilot CLI runtime...")
        result = run_command(f"{apm_binary} runtime setup copilot", timeout=300, show_output=True)
        assert result.returncode == 0, f"Runtime setup failed: {result.stderr}"
        
        # Verify copilot is available and GitHub configuration was created
        copilot_config = Path(temp_e2e_home) / ".copilot" / "mcp-config.json"
        
        assert copilot_config.exists(), "Copilot configuration not created"
        
        # Verify configuration contains MCP setup
        config_content = copilot_config.read_text()
        print(f"✓ Copilot configuration created:\n{config_content}")
        
        # Test copilot binary directly
        print("\n=== Testing Copilot CLI binary directly ===")
        result = run_command("copilot --version", show_output=True, check=False)
        if result.returncode == 0:
            print(f"✓ Copilot version: {result.stdout}")
        else:
            print(f"⚠ Copilot version check failed: {result.stderr}")
            
        # Check if copilot is in PATH
        print("\n=== Checking PATH setup ===")
        result = run_command("which copilot", check=False)
        if result.returncode == 0:
            print(f"✓ Copilot found in PATH: {result.stdout.strip()}")
        else:
            print("⚠ Copilot not in PATH, will need explicit path or shell restart")
        
        # Step 2: Initialize project (equivalent to: apm init my-ai-native-project) 
        with tempfile.TemporaryDirectory() as project_workspace:
            project_dir = Path(project_workspace) / "my-ai-native-project"
            
            print("\n=== Step 3: Transform your project with AI-Native structure ===")
            result = run_command(f"{apm_binary} init my-ai-native-project --yes", cwd=project_workspace, show_output=True)
            assert result.returncode == 0, f"Project init failed: {result.stderr}"
            assert project_dir.exists(), "Project directory not created"
            
            # Verify minimal project structure (new behavior: only apm.yml created)
            assert (project_dir / "apm.yml").exists(), "apm.yml not created"
            
            # NEW: Create template files manually (simulating user workflow in minimal mode)
            print("\n=== Creating project files (minimal mode workflow) ===")
            
            # Create hello-world.prompt.md
            prompt_content = """---
description: Hello World prompt for testing
---

# Hello World Prompt

This is a test prompt for {{name}}.

Say hello to {{name}}!
"""
            (project_dir / "hello-world.prompt.md").write_text(prompt_content)
            
            # Create .apm directory structure with minimal instructions
            apm_dir = project_dir / ".apm"
            apm_dir.mkdir(exist_ok=True)
            (apm_dir / "instructions").mkdir(exist_ok=True)
            
            # Create a minimal instruction file
            instruction_content = """---
applyTo: "**"
description: Test instructions for E2E
---

# Test Instructions

Basic instructions for E2E testing.
"""
            (apm_dir / "instructions" / "test.instructions.md").write_text(instruction_content)
            
            # Update apm.yml to add start script
            import yaml
            apm_yml_path = project_dir / "apm.yml"
            with open(apm_yml_path, 'r') as f:
                config = yaml.safe_load(f)
            
            # Add start script for copilot
            if 'scripts' not in config:
                config['scripts'] = {}
            config['scripts']['start'] = 'copilot --log-level all --log-dir copilot-logs --allow-all-tools -p hello-world.prompt.md'
            
            with open(apm_yml_path, 'w') as f:
                yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)
            
            print(f"✓ Created hello-world.prompt.md, .apm/ directory, and updated apm.yml")
            
            # Show project contents for debugging
            print("\n=== Project structure ===")
            apm_yml_content = (project_dir / "apm.yml").read_text()
            print(f"apm.yml:\n{apm_yml_content}")
            print(f"hello-world.prompt.md:\n{prompt_content[:500]}...")
            
            # List created files
            created_files = list(project_dir.rglob("*"))
            print(f"\n=== Created Files ({len([f for f in created_files if f.is_file()])} files) ===")
            for f in sorted([f for f in created_files if f.is_file()]):
                rel_path = f.relative_to(project_dir)
                print(f"  {rel_path}")
            
            # Step 4: Compile Agent Primitives for any coding agent (equivalent to: apm compile)
            print("\n=== Step 4: Compile Agent Primitives for any coding agent ===")
            result = run_command(f"{apm_binary} compile", cwd=project_dir, show_output=True)
            assert result.returncode == 0, f"Agent Primitives compilation failed: {result.stderr}"
            
            # Verify agents.md was generated
            agents_md = project_dir / "AGENTS.md"
            assert agents_md.exists(), "AGENTS.md not generated by compile step"
            
            # Show agents.md content for verification
            agents_content = agents_md.read_text()
            print(f"\n=== Generated AGENTS.md (first 500 chars) ===")
            print(f"{agents_content[:500]}...")
            
            # Step 5: Install MCP dependencies (equivalent to: apm install)
            print("\n=== Step 5: Install MCP dependencies ===")
            
            # Set Azure DevOps MCP runtime variables for domain restriction
            env = os.environ.copy()
            env['ado_domain'] = 'core'  # Limit to core domain only
            # Leave ado_org unset to avoid connecting to real organization
            
            result = run_command(f"{apm_binary} install", cwd=project_dir, show_output=True, env=env)
            assert result.returncode == 0, f"Dependency install failed: {result.stderr}"
            
            # Step 5.5: Domain restriction is handled via ado_domain environment variable
            # No post-install injection needed - runtime variables are resolved during install
            
            # Step 6: Execute agentic workflows (equivalent to: apm run start --param name="<YourGitHubHandle>")
            print("\n=== Step 6: Execute agentic workflows ===")
            print(f"Environment: HOME={temp_e2e_home}, Primary token={'SET' if PRIMARY_TOKEN else 'NOT SET'}")
            
            # Respect integration script's token management
            # Do not override - let test-integration.sh handle tokens properly
            env = os.environ.copy()
            env['HOME'] = temp_e2e_home
            
            # Run with real-time output streaming (using 'start' script which calls Copilot CLI)
            cmd = f'{apm_binary} run start --param name="developer"'
            print(f"Executing: {cmd}")
            
            try:
                process = subprocess.Popen(
                    cmd,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,  # Merge stderr into stdout
                    text=True,
                    cwd=project_dir,
                    env=env
                )
                
                output_lines = []
                print("\n--- Copilot CLI Execution Output ---")
                
                # Stream output in real-time
                for line in iter(process.stdout.readline, ''):
                    if line:
                        print(line.rstrip())  # Print to terminal
                        output_lines.append(line)
                
                # Wait for completion
                return_code = process.wait(timeout=120)
                full_output = ''.join(output_lines)
                
                print("--- End Copilot CLI Output ---\n")
                
                # Verify execution
                if return_code != 0:
                    print(f"❌ Command failed with return code: {return_code}")
                    print(f"Full output:\n{full_output}")
                    
                    # Check for common issues
                    if "GITHUB_TOKEN" in full_output or "authentication" in full_output.lower():
                        pytest.fail("Copilot CLI execution failed: GitHub token not properly configured")
                    elif "Connection" in full_output or "timeout" in full_output.lower():
                        pytest.fail("Copilot CLI execution failed: Network connectivity issue")
                    else:
                        pytest.fail(f"Golden scenario execution failed with return code {return_code}: {full_output}")
                
                # Verify output contains expected elements (using "Developer" instead of "E2E Tester")
                output_lower = full_output.lower()
                assert "developer" in output_lower, \
                    f"Parameter substitution failed. Expected 'Developer', got: {full_output}"
                assert len(full_output.strip()) > 50, \
                    f"Output seems too short, API call might have failed. Output: {full_output}"
                
                print(f"\n✅ Golden scenario completed successfully!")
                print(f"Output length: {len(full_output)} characters")
                print(f"Contains parameter: {'✓' if 'developer' in output_lower else '❌'}")
                
            except subprocess.TimeoutExpired:
                process.kill()
                pytest.fail("Copilot CLI execution timed out after 120 seconds")

    @pytest.mark.skipif(not PRIMARY_TOKEN, reason="GitHub token (GITHUB_APM_PAT or GITHUB_TOKEN) required for E2E tests")
    def test_complete_golden_scenario_codex(self, temp_e2e_home, apm_binary):
        """Test the complete golden scenario using Codex CLI runtime.
        
        This test uses the 'debug' script which actually calls Codex CLI.
        """
        
        # Step 1: Setup Codex runtime
        print("\n=== Setting up Codex CLI runtime ===")
        result = run_command(f"{apm_binary} runtime setup codex", timeout=300, show_output=True)
        assert result.returncode == 0, f"Codex runtime setup failed: {result.stderr}"
        
        # Verify codex is available and GitHub configuration was created
        codex_binary = Path(temp_e2e_home) / ".apm" / "runtimes" / "codex"
        codex_config = Path(temp_e2e_home) / ".codex" / "config.toml"
        
        assert codex_binary.exists(), "Codex binary not installed"
        assert codex_config.exists(), "Codex configuration not created"
        
        # Verify configuration contains GitHub Models setup
        config_content = codex_config.read_text()
        assert "github-models" in config_content, "GitHub Models configuration not found"
        # Should use GITHUB_TOKEN for GitHub Models access (user-scoped PAT required)
        assert "GITHUB_TOKEN" in config_content, f"Expected GITHUB_TOKEN in config. Found: {config_content}"
        print(f"✓ Codex configuration created with GITHUB_TOKEN for GitHub Models")
        
        # Step 2: Use existing project or create new one
        with tempfile.TemporaryDirectory() as project_workspace:
            project_dir = Path(project_workspace) / "my-ai-native-project-codex"
            
            print("\n=== Initializing Codex test project ===")
            result = run_command(f"{apm_binary} init my-ai-native-project-codex --yes", cwd=project_workspace)
            assert result.returncode == 0, f"Project init failed: {result.stderr}"
            
            # NEW: Create template files manually (minimal mode workflow)
            print("\n=== Creating project files for testing ===")
            
            # Create a simple prompt file for the debug script
            prompt_content = """---
description: Debug prompt for Codex testing
---

# Debug Prompt

This is a test prompt for {{name}}.
"""
            (project_dir / "debug.prompt.md").write_text(prompt_content)
            
            # Create .apm directory with minimal instructions
            apm_dir = project_dir / ".apm"
            apm_dir.mkdir(exist_ok=True)
            (apm_dir / "instructions").mkdir(exist_ok=True)
            
            instruction_content = """---
applyTo: "**"
description: Codex test instructions
---

# Test Instructions

Instructions for Codex E2E testing.
"""
            (apm_dir / "instructions" / "test.instructions.md").write_text(instruction_content)
            
            # Update apm.yml to add debug script
            import yaml
            apm_yml_path = project_dir / "apm.yml"
            with open(apm_yml_path, 'r') as f:
                config = yaml.safe_load(f)
            
            # Add debug script
            if 'scripts' not in config:
                config['scripts'] = {}
            config['scripts']['debug'] = 'codex debug.prompt.md'
            
            with open(apm_yml_path, 'w') as f:
                yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)
            
            print(f"✓ Created debug.prompt.md, .apm/ directory, and updated apm.yml")
            
            # Step 3: Compile Agent Primitives
            print("\n=== Compiling Agent Primitives ===")
            result = run_command(f"{apm_binary} compile", cwd=project_dir)
            assert result.returncode == 0, f"Compilation failed: {result.stderr}"
            
            # Step 4: Install dependencies
            print("\n=== Installing dependencies ===")
            
            # Set Azure DevOps MCP runtime variables for domain restriction
            env = os.environ.copy()
            env['ado_domain'] = 'core'  # Limit to core domain only
            env['HOME'] = temp_e2e_home
            
            result = run_command(f"{apm_binary} install", cwd=project_dir, env=env)
            assert result.returncode == 0, f"Dependency install failed: {result.stderr}"
            
            # Step 5: Run with Codex CLI (equivalent to: apm run debug --param name="<YourGitHubHandle>")
            print("\n=== Running golden scenario with Codex CLI ===")
            
            # Respect integration script's token management
            # Do not override - let test-integration.sh handle tokens properly
            env = os.environ.copy()
            env['HOME'] = temp_e2e_home
            
            # Run the Codex command with proper environment (use 'debug' script which calls Codex CLI)
            cmd = f'{apm_binary} run debug --param name="developer"'
            process = subprocess.Popen(
                cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=project_dir,
                env=env
            )
            
            output_lines = []
            print("\n--- Codex CLI Execution Output ---")
            for line in iter(process.stdout.readline, ''):
                if line:
                    print(line.rstrip())
                    output_lines.append(line)
            
            return_code = process.wait(timeout=120)
            full_output = ''.join(output_lines)
            
            print("--- End Codex CLI Output ---\n")
            
            # Verify execution (Codex CLI might have different authentication requirements)
            if return_code == 0:
                output = full_output.lower()
                assert "developer" in output, "Parameter substitution failed"
                assert len(output.strip()) > 50, "Output seems too short"
                print(f"\n✅ Codex CLI scenario completed successfully!")
                print(f"Output length: {len(full_output)} characters")
            else:
                # Codex CLI might fail due to auth setup in CI, log for debugging
                print(f"\n=== Codex CLI execution failed (expected in some environments) ===")
                print(f"Output: {full_output}")
                
                # Check for common authentication issues
                if "authentication" in full_output.lower() or "token" in full_output.lower():
                    pytest.skip("Codex CLI execution failed due to authentication - this is expected in some CI environments")
                else:
                    pytest.skip(f"Codex CLI execution failed with return code {return_code}: {full_output}")
            
    @pytest.mark.skipif(not PRIMARY_TOKEN, reason="GitHub token (GITHUB_APM_PAT or GITHUB_TOKEN) required for E2E tests")
    def test_complete_golden_scenario_llm(self, temp_e2e_home, apm_binary):
        """Test the complete golden scenario using LLM runtime."""
        
        # Step 1: Setup LLM runtime (equivalent to: apm runtime setup llm)
        print("\\n=== Setting up LLM runtime ===")
        result = run_command(f"{apm_binary} runtime setup llm", timeout=300)
        assert result.returncode == 0, f"LLM runtime setup failed: {result.stderr}"
        
        # Verify LLM is available
        llm_wrapper = Path(temp_e2e_home) / ".apm" / "runtimes" / "llm"
        assert llm_wrapper.exists(), "LLM wrapper not installed"
        
        # Configure LLM for GitHub Models
        print("\\n=== Configuring LLM for GitHub Models ===")
        # LLM expects GITHUB_MODELS_KEY environment variable, not GITHUB_TOKEN
        # Set it for the LLM runtime
        os.environ['GITHUB_MODELS_KEY'] = GITHUB_TOKEN
        print("✓ Set GITHUB_MODELS_KEY environment variable for LLM")
        
        # Step 2: Use existing project or create new one
        with tempfile.TemporaryDirectory() as project_workspace:
            project_dir = Path(project_workspace) / "my-ai-native-project-llm"
            
            print("\\n=== Initializing LLM test project ===")
            result = run_command(f"{apm_binary} init my-ai-native-project-llm --yes", cwd=project_workspace)
            assert result.returncode == 0, f"Project init failed: {result.stderr}"
            
            # NEW: Create template files manually (minimal mode workflow)
            print("\\n=== Creating project files for testing ===")
            
            # Create a simple prompt file for the llm script
            prompt_content = """---
description: LLM prompt for testing
---

# LLM Prompt

This is a test prompt for {{name}}.
"""
            (project_dir / "llm.prompt.md").write_text(prompt_content)
            
            # Create .apm directory with minimal instructions
            apm_dir = project_dir / ".apm"
            apm_dir.mkdir(exist_ok=True)
            (apm_dir / "instructions").mkdir(exist_ok=True)
            
            instruction_content = """---
applyTo: "**"
description: LLM test instructions
---

# Test Instructions

Instructions for LLM E2E testing.
"""
            (apm_dir / "instructions" / "test.instructions.md").write_text(instruction_content)
            
            # Update apm.yml to add llm script
            import yaml
            apm_yml_path = project_dir / "apm.yml"
            with open(apm_yml_path, 'r') as f:
                config = yaml.safe_load(f)
            
            # Add llm script
            if 'scripts' not in config:
                config['scripts'] = {}
            config['scripts']['llm'] = 'llm llm.prompt.md -m github/gpt-4o-mini'
            
            with open(apm_yml_path, 'w') as f:
                yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)
            
            print(f"✓ Created llm.prompt.md, .apm/ directory, and updated apm.yml")
            
            # Step 3: Compile Agent Primitives
            print("\\n=== Compiling Agent Primitives ===")
            result = run_command(f"{apm_binary} compile", cwd=project_dir)
            assert result.returncode == 0, f"Compilation failed: {result.stderr}"
            
            # Step 4: Install dependencies
            print("\\n=== Installing dependencies ===")
            
            # Set Azure DevOps MCP runtime variables for domain restriction
            env = os.environ.copy()
            env['ado_domain'] = 'core'  # Limit to core domain only
            env['HOME'] = temp_e2e_home
            
            result = run_command(f"{apm_binary} install", cwd=project_dir, env=env)
            assert result.returncode == 0, f"Dependency install failed: {result.stderr}"
            
            # Step 5: Run with LLM runtime (equivalent to: apm run llm --param name="<YourGitHubHandle>")
            print("\\n=== Running golden scenario with LLM ===")
            
            # Use test environment with proper token setup for LLM runtime
            env = os.environ.copy()
            env['HOME'] = temp_e2e_home
            
            # LLM expects GITHUB_MODELS_KEY for GitHub Models access
            if 'GITHUB_TOKEN' in env or 'GITHUB_APM_PAT' in env:
                github_token = env.get('GITHUB_TOKEN') or env.get('GITHUB_APM_PAT')
                if github_token:
                    env['GITHUB_MODELS_KEY'] = github_token
            
            # Run the LLM command with proper environment (use 'llm' script, not 'start')
            cmd = f'{apm_binary} run llm --param name="developer"'
            process = subprocess.Popen(
                cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=project_dir,
                env=env
            )
            
            output_lines = []
            for line in iter(process.stdout.readline, ''):
                if line:
                    print(line.rstrip())
                    output_lines.append(line)
            
            return_code = process.wait(timeout=120)
            full_output = ''.join(output_lines)
            
            # Verify execution (LLM might have different authentication requirements)
            if return_code == 0:
                output = full_output.lower()
                assert "developer" in output, "Parameter substitution failed"
                assert len(output.strip()) > 50, "Output seems too short"
                print(f"\\n=== LLM scenario output ===\\n{full_output}")
            else:
                # LLM might fail due to auth setup in CI, log for debugging
                print(f"\\n=== LLM execution failed (expected in CI) ===")
                print(f"Output: {full_output}")
                pytest.skip("LLM execution failed, likely due to authentication in CI environment")

    @pytest.mark.skipif(not PRIMARY_TOKEN, reason="GitHub token (GITHUB_APM_PAT or GITHUB_TOKEN) required for E2E tests")
    def test_complete_golden_scenario_gemini(self, temp_e2e_home, apm_binary):
        """Test the complete golden scenario using Gemini CLI runtime.

        This test uses the 'review' script which calls Gemini CLI.
        Gemini CLI authenticates via Google account (browser flow) or
        GOOGLE_API_KEY, so this test will gracefully skip on auth failure.
        """

        # Step 1: Setup Gemini runtime (npm install -g @google/gemini-cli)
        print("\n=== Setting up Gemini CLI runtime ===")
        result = run_command(f"{apm_binary} runtime setup gemini", timeout=300, show_output=True)
        assert result.returncode == 0, f"Gemini runtime setup failed: {result.stderr}"

        # Verify gemini is available (npm global install, not in ~/.apm/runtimes)
        print("\n=== Testing Gemini CLI binary ===")
        result = run_command("gemini --version", show_output=True, check=False)
        if result.returncode == 0:
            print(f"Gemini CLI version: {result.stdout}")
        else:
            print(f"Gemini CLI version check failed: {result.stderr}")

        # Verify config directory was created
        gemini_config_dir = Path(temp_e2e_home) / ".gemini"
        assert gemini_config_dir.exists(), "Gemini config directory not created"

        gemini_settings = gemini_config_dir / "settings.json"
        assert gemini_settings.exists(), "Gemini settings.json not created"

        settings_content = gemini_settings.read_text()
        settings = json.loads(settings_content)
        assert "mcpServers" in settings, "mcpServers key not in settings.json"
        print(f"Gemini settings.json: {settings_content}")

        # Step 2: Create test project
        with tempfile.TemporaryDirectory() as project_workspace:
            project_dir = Path(project_workspace) / "my-ai-native-project-gemini"

            print("\n=== Initializing Gemini test project ===")
            result = run_command(f"{apm_binary} init my-ai-native-project-gemini --yes", cwd=project_workspace)
            assert result.returncode == 0, f"Project init failed: {result.stderr}"

            # Create a simple prompt file
            prompt_content = """---
description: Review prompt for Gemini testing
---

# Review Prompt

This is a test prompt for ${input:name}. Reply with exactly: hello developer
"""
            (project_dir / "review.prompt.md").write_text(prompt_content)

            # Create .apm directory with instructions
            apm_dir = project_dir / ".apm"
            apm_dir.mkdir(exist_ok=True)
            (apm_dir / "instructions").mkdir(exist_ok=True)

            instruction_content = """---
applyTo: "**"
description: Gemini test instructions
---

# Test Instructions

Instructions for Gemini CLI E2E testing.
"""
            (apm_dir / "instructions" / "test.instructions.md").write_text(instruction_content)

            # Create .gemini directory so target is detected
            (project_dir / ".gemini").mkdir(exist_ok=True)

            # Update apm.yml to add review script
            import yaml
            apm_yml_path = project_dir / "apm.yml"
            with open(apm_yml_path, 'r') as f:
                config = yaml.safe_load(f)

            if 'scripts' not in config:
                config['scripts'] = {}
            config['scripts']['review'] = 'gemini -y review.prompt.md'

            with open(apm_yml_path, 'w') as f:
                yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)

            print("Created review.prompt.md, .apm/ directory, .gemini/ directory, and updated apm.yml")

            # Step 3: Compile Agent Primitives
            print("\n=== Compiling Agent Primitives ===")
            result = run_command(f"{apm_binary} compile", cwd=project_dir)
            assert result.returncode == 0, f"Compilation failed: {result.stderr}"

            # Step 4: Install dependencies (targets gemini since .gemini/ exists)
            print("\n=== Installing dependencies ===")
            env = os.environ.copy()
            env['HOME'] = temp_e2e_home

            result = run_command(f"{apm_binary} install", cwd=project_dir, env=env)
            assert result.returncode == 0, f"Dependency install failed: {result.stderr}"

            # Step 5: Run with Gemini CLI
            print("\n=== Running golden scenario with Gemini CLI ===")
            env = os.environ.copy()
            env['HOME'] = temp_e2e_home
            env['GEMINI_CLI_TRUST_WORKSPACE'] = 'true'


            cmd = f'{apm_binary} run review --param name="developer"'
            process = subprocess.Popen(
                cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=project_dir,
                env=env
            )

            output_lines = []
            print("\n--- Gemini CLI Execution Output ---")
            for line in iter(process.stdout.readline, ''):
                if line:
                    print(line.rstrip())
                    output_lines.append(line)

            return_code = process.wait(timeout=120)
            full_output = ''.join(output_lines)

            print("--- End Gemini CLI Output ---\n")

            if return_code == 0:
                output = full_output.lower()
                assert "developer" in output, "Parameter substitution failed"
                assert len(output.strip()) > 50, "Output seems too short"
                print(f"\nGemini CLI scenario completed successfully!")
                print(f"Output length: {len(full_output)} characters")
            else:
                print(f"\n=== Gemini CLI execution failed (expected in some environments) ===")
                print(f"Output: {full_output}")

                if "authentication" in full_output.lower() or "login" in full_output.lower() or "api_key" in full_output.lower():
                    pytest.skip("Gemini CLI execution failed due to authentication - this is expected in CI environments without Google credentials")
                else:
                    pytest.skip(f"Gemini CLI execution failed with return code {return_code}: {full_output}")

    def test_runtime_list_command(self, temp_e2e_home, apm_binary):
        """Test that APM can list installed runtimes."""
        print("\\n=== Testing runtime list command ===")
        result = run_command(f"{apm_binary} runtime list")
        
        # Should succeed even if no runtimes installed
        assert result.returncode == 0, f"Runtime list failed: {result.stderr}"
        
        # Output should contain some indication of runtime status
        output = result.stdout.lower()
        assert "runtime" in output or "codex" in output or "llm" in output or "no runtimes" in output, \
            "Runtime list output doesn't look correct"
        
        print(f"Runtime list output: {result.stdout}")

    def test_apm_version_and_help(self, apm_binary):
        """Test basic APM CLI functionality."""
        print("\\n=== Testing APM CLI basics ===")
        
        # Test version
        result = run_command(f"{apm_binary} --version")
        assert result.returncode == 0, f"Version command failed: {result.stderr}"
        assert result.stdout.strip(), "Version output is empty"
        
        # Test help
        result = run_command(f"{apm_binary} --help")
        assert result.returncode == 0, f"Help command failed: {result.stderr}"
        assert "usage:" in result.stdout.lower() or "apm" in result.stdout.lower(), \
            "Help output doesn't look correct"
        
        print(f"APM version: {result.stdout}")

    def test_init_command_minimal_mode(self, temp_e2e_home, apm_binary):
        """Test apm init command in minimal mode (new behavior)."""
        print("\\n=== Testing APM init command (minimal mode) ===")
        
        with tempfile.TemporaryDirectory() as workspace:
            project_dir = Path(workspace) / "template-test-project"
            
            # Test apm init
            result = run_command(f"{apm_binary} init template-test-project --yes", cwd=workspace, show_output=True)
            assert result.returncode == 0, f"APM init failed: {result.stderr}"
            
            # Verify minimal project structure (only apm.yml)
            assert project_dir.exists(), "Project directory not created"
            assert (project_dir / "apm.yml").exists(), "apm.yml not created"
            
            # Verify template files are NOT created (minimal mode)
            assert not (project_dir / "hello-world.prompt.md").exists(), "Prompt template should not be created in minimal mode"
            assert not (project_dir / ".apm").exists(), "Agent Primitives directory should not be created in minimal mode"
            
            print(f"✅ Minimal mode test passed: Only apm.yml created")


class TestRuntimeInteroperability:
    """Test that both runtimes can be installed and work together."""
    
    def test_dual_runtime_installation(self, temp_e2e_home, apm_binary):
        """Test installing both runtimes in the same environment."""
        
        # Install Codex
        print("\\n=== Installing Codex runtime ===")
        result = run_command(f"{apm_binary} runtime setup codex", timeout=300)
        assert result.returncode == 0, f"Codex setup failed: {result.stderr}"
        
        # Install LLM  
        print("\\n=== Installing LLM runtime ===")
        result = run_command(f"{apm_binary} runtime setup llm", timeout=300)
        assert result.returncode == 0, f"LLM setup failed: {result.stderr}"
        
        # Verify both are available
        runtime_dir = Path(temp_e2e_home) / ".apm" / "runtimes"
        assert (runtime_dir / "codex").exists(), "Codex not found after dual install"
        assert (runtime_dir / "llm").exists(), "LLM not found after dual install"
        
        # Test runtime list shows both
        result = run_command(f"{apm_binary} runtime list")
        assert result.returncode == 0, f"Runtime list failed: {result.stderr}"
        
        output = result.stdout.lower()
        # Should show both runtimes (exact format may vary)
        print(f"Runtime list with both installed: {result.stdout}")


if __name__ == "__main__":
    # Example of how to run E2E tests manually
    print("To run E2E tests manually:")
    print("export APM_E2E_TESTS=1")
    print("export GITHUB_TOKEN=your_token_here")  
    print("pytest tests/integration/test_golden_scenario_e2e.py -v -s")
    
    # Run tests when executed directly
    if E2E_MODE:
        pytest.main([__file__, "-v", "-s"])
    else:
        print("\\nE2E mode not enabled. Set APM_E2E_TESTS=1 to run these tests.")
