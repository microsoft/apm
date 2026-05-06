"""Docker arguments processing utilities for MCP configuration."""

from typing import Dict, List, Tuple  # noqa: F401, UP035


class DockerArgsProcessor:
    """Handles Docker argument processing with deduplication."""

    @staticmethod
    def process_docker_args(base_args: list[str], env_vars: dict[str, str]) -> list[str]:
        """Process Docker arguments with environment variable deduplication and required flags.

        Args:
            base_args: Base Docker arguments list.
            env_vars: Environment variables to inject.

        Returns:
            Updated arguments with environment variables injected without duplicates and required flags.
        """
        result = []
        env_vars_added = set()
        has_interactive = False
        has_rm = False

        # Check for existing -i and --rm flags
        for i, arg in enumerate(base_args):  # noqa: B007
            if arg == "-i" or arg == "--interactive":  # noqa: PLR1714
                has_interactive = True
            elif arg == "--rm":
                has_rm = True

        for arg in base_args:
            result.append(arg)

            # When we encounter "run", inject required flags and environment variables
            if arg == "run":
                # Add -i flag if not present
                if not has_interactive:
                    result.append("-i")

                # Add --rm flag if not present
                if not has_rm:
                    result.append("--rm")

                # Add environment variables
                for env_name, env_value in env_vars.items():
                    if env_name not in env_vars_added:
                        result.extend(["-e", f"{env_name}={env_value}"])
                        env_vars_added.add(env_name)

        return result

    @staticmethod
    def extract_env_vars_from_args(args: list[str]) -> tuple[list[str], dict[str, str]]:
        """Extract environment variables from Docker args.

        Args:
            args: Docker arguments that may contain -e flags.

        Returns:
            Tuple of (clean_args, env_vars) where clean_args has -e flags removed
            and env_vars contains the extracted environment variables.
        """
        clean_args = []
        env_vars = {}
        i = 0

        while i < len(args):
            if args[i] == "-e" and i + 1 < len(args):
                env_spec = args[i + 1]
                if "=" in env_spec:
                    key, value = env_spec.split("=", 1)
                    env_vars[key] = value
                else:
                    env_vars[env_spec] = "${" + env_spec + "}"
                i += 2  # Skip both -e and the env spec
            else:
                clean_args.append(args[i])
                i += 1

        return clean_args, env_vars

    @staticmethod
    def merge_env_vars(existing_env: dict[str, str], new_env: dict[str, str]) -> dict[str, str]:
        """Merge environment variables, prioritizing resolved values over templates.

        Args:
            existing_env: Existing environment variables (often templates from registry).
            new_env: New environment variables to merge (resolved actual values).

        Returns:
            Merged environment variables with resolved values taking precedence.
        """
        merged = existing_env.copy()
        merged.update(new_env)  # Resolved values take precedence over templates
        return merged
