"""
End-to-end test for a typical 2-Minute Guardrailing flow.

Exercises a guardrailing workflow with mixed package types:
1. apm init my-project && cd my-project
2. apm install microsoft/apm-sample-package
3. apm install github/awesome-copilot/instructions/code-review-generic.instructions.md
4. apm compile
5. apm run design-review

This validates that:
- Multiple APM packages can be installed (full package + virtual instruction)
- AGENTS.md is generated with combined instructions from both packages
- Prompts from installed packages can be executed
"""

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

# Skip all tests in this module if not in E2E mode
E2E_MODE = os.environ.get("APM_E2E_TESTS", "").lower() in ("1", "true", "yes")

# Token detection for test requirements
GITHUB_APM_PAT = os.environ.get("GITHUB_APM_PAT")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
PRIMARY_TOKEN = GITHUB_APM_PAT or GITHUB_TOKEN

pytestmark = pytest.mark.skipif(
    not E2E_MODE, reason="E2E tests only run when APM_E2E_TESTS=1 is set"
)


def run_command(
    cmd, check=True, capture_output=True, timeout=180, cwd=None, show_output=False, env=None
):
    """Run a shell command with proper error handling."""
    try:
        if show_output:
            print(f"\n>>> Running command: {cmd}")
            result = subprocess.run(
                cmd,
                shell=True,
                check=check,
                capture_output=False,
                text=True,
                timeout=timeout,
                cwd=cwd,
                env=env,
                encoding="utf-8",
                errors="replace",
            )
            result_capture = subprocess.run(
                cmd,
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
                env=env,
                encoding="utf-8",
                errors="replace",
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
                env=env,
                encoding="utf-8",
                errors="replace",
            )
        return result
    except subprocess.TimeoutExpired:
        pytest.fail(f"Command timed out after {timeout}s: {cmd}")
    except subprocess.CalledProcessError as e:
        pytest.fail(f"Command failed: {cmd}\nStdout: {e.stdout}\nStderr: {e.stderr}")


@pytest.fixture(scope="module")
def apm_binary():
    """Get path to APM binary for testing."""
    possible_paths = [
        "apm",
        "./apm",
        "./dist/apm",
        Path(__file__).parent.parent.parent / "dist" / "apm",
    ]

    for path in possible_paths:
        try:
            result = subprocess.run([str(path), "--version"], capture_output=True, text=True)
            if result.returncode == 0:
                return str(path)
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue

    pytest.skip("APM binary not found. Build it first with: python -m build")


class TestGuardrailingHeroScenario:
    """Test README Hero Scenario 2: 2-Minute Guardrailing"""

    @pytest.mark.skipif(not PRIMARY_TOKEN, reason="GitHub token required for E2E tests")
    def test_2_minute_guardrailing_flow(self, apm_binary):
        """Test the exact 2-minute guardrailing flow from README.

        Validates:
        1. apm init my-project creates minimal project
        2. apm install microsoft/apm-sample-package succeeds
        3. apm install github/awesome-copilot/instructions/code-review-generic.instructions.md succeeds
        4. apm compile generates AGENTS.md with instructions from both packages
        5. apm run design-review executes prompt from first installed package
        """

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as workspace:
            # Step 1: apm init my-project
            print("\n=== Step 1: apm init my-project ===")
            result = run_command(
                f"{apm_binary} init my-project --yes", cwd=workspace, show_output=True
            )
            assert result.returncode == 0, f"Project init failed: {result.stderr}"

            project_dir = Path(workspace) / "my-project"
            assert project_dir.exists(), "Project directory not created"
            assert (project_dir / "apm.yml").exists(), "apm.yml not created"

            print("[OK] Project initialized")

            # Step 2: apm install microsoft/apm-sample-package
            print("\n=== Step 2: apm install microsoft/apm-sample-package ===")
            env = os.environ.copy()
            result = run_command(
                f"{apm_binary} install microsoft/apm-sample-package",
                cwd=project_dir,
                show_output=True,
                env=env,
            )
            assert result.returncode == 0, f"design-guidelines install failed: {result.stderr}"

            # Verify installation
            design_pkg = project_dir / "apm_modules" / "microsoft" / "apm-sample-package"
            assert design_pkg.exists(), "design-guidelines package not installed"
            assert (design_pkg / "apm.yml").exists(), "design-guidelines apm.yml not found"

            print("[OK] design-guidelines installed")

            # Step 3: apm install github/awesome-copilot/instructions/code-review-generic.instructions.md
            print(
                "\n=== Step 3: apm install github/awesome-copilot/instructions/code-review-generic.instructions.md ==="
            )
            result = run_command(
                f"{apm_binary} install github/awesome-copilot/instructions/code-review-generic.instructions.md",
                cwd=project_dir,
                show_output=True,
                env=env,
            )
            assert result.returncode == 0, f"instruction package install failed: {result.stderr}"

            # Verify installation - virtual file packages use flattened name: owner/repo-name-file-stem
            instruction_pkg = (
                project_dir / "apm_modules" / "github" / "awesome-copilot-code-review-generic"
            )
            assert instruction_pkg.exists(), "instruction package not installed"

            # Verify the instruction file was actually downloaded
            instruction_files = list(instruction_pkg.rglob("*.instructions.md"))
            assert len(instruction_files) > 0, (
                "instruction file not downloaded into virtual package"
            )

            print("[OK] code-review-generic instruction installed")

            # Step 4: apm compile
            print("\n=== Step 4: apm compile ===")
            result = run_command(f"{apm_binary} compile", cwd=project_dir, show_output=True)
            assert result.returncode == 0, f"Compilation failed: {result.stderr}"

            # Verify AGENTS.md was generated
            agents_md = project_dir / "AGENTS.md"
            assert agents_md.exists(), "AGENTS.md not generated"

            # Verify AGENTS.md contains instructions from both packages
            agents_content = agents_md.read_text()
            assert "design" in agents_content.lower(), (
                "AGENTS.md doesn't contain design-related content from apm-sample-package"
            )
            assert "review" in agents_content.lower() or "code" in agents_content.lower(), (
                "AGENTS.md doesn't contain code-review content from awesome-copilot"
            )

            print(f"[OK] AGENTS.md generated ({len(agents_content)} bytes)")
            print("  Contains design instructions: [OK]")
            print("  Contains code-review instructions: [OK]")

            # Step 5: apm run design-review
            print("\n=== Step 5: apm run design-review ===")

            # Use early termination pattern - we only need to verify prompt starts correctly
            # Don't wait for full Copilot CLI execution (takes minutes)
            process = subprocess.Popen(
                f"{apm_binary} run design-review",
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=project_dir,
                env=env,
                encoding="utf-8",
                errors="replace",
            )

            # Monitor output for success signals
            output_lines = []
            prompt_started = False

            try:
                for line in iter(process.stdout.readline, ""):
                    if not line:
                        break

                    output_lines.append(line.rstrip())
                    print(f"  {line.rstrip()}")

                    # Look for signals that prompt execution started successfully
                    if any(
                        signal in line
                        for signal in [
                            "Subprocess execution:",  # Codex about to run
                        ]
                    ):
                        prompt_started = True
                        print("[OK] design-review prompt execution started")
                        break

                # Terminate the process gracefully
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()

            except Exception as e:
                process.kill()
                process.wait()
                pytest.fail(f"Error monitoring design-review execution: {e}")
            finally:
                if process.stdout:
                    process.stdout.close()

            # Verify prompt was found and started
            full_output = "\n".join(output_lines)
            assert prompt_started or "design-review" in full_output, (
                f"Prompt execution didn't start correctly. Output:\n{full_output}"
            )

            print("[OK] design-review prompt found and started successfully")

            print("\n=== 2-Minute Guardrailing Hero Scenario: PASSED ===")
            print("[OK] Project initialization")
            print("[OK] Multiple APM package installation")
            print("[OK] AGENTS.md compilation with combined instructions")
            print("[OK] Prompt execution from installed package")


if __name__ == "__main__":
    if E2E_MODE:
        pytest.main([__file__, "-v", "-s"])
    else:
        print("E2E mode not enabled. Set APM_E2E_TESTS=1 to run these tests.")
