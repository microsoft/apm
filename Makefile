# Minimal Makefile -- DX shortcut for the NOTICE-file generator.
# Add other targets here as the project grows.

.PHONY: notice notice-check

# Regenerate NOTICE.md from pyproject.toml + scripts/notice-metadata.yaml.
# Run this whenever you add / remove / bump a runtime dependency.
notice:
	uv run python scripts/generate-notice.py

# Same check that .github/workflows/notice-drift.yml runs in CI; useful
# for verifying locally before pushing.
notice-check:
	uv run python scripts/generate-notice.py --check
