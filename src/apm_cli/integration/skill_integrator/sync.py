"""Skill integration functionality for APM packages (Claude Code & Cursor support)."""

import filecmp
import hashlib  # noqa: F401
import re
import shutil
from dataclasses import dataclass
from datetime import datetime  # noqa: F401
from pathlib import Path

import frontmatter  # noqa: F401

from apm_cli.integration.base_integrator import BaseIntegrator


# DEPRECATED -- use IntegrationResult directly for new code.
# Kept for backward compatibility. The fields map as follows:
# skill_created -> IntegrationResult.skill_created
# sub_skills_promoted -> IntegrationResult.sub_skills_promoted
# skill_path, references_copied -> not mapped (skill-internal)
# Sync helpers are implemented as SkillIntegrator methods in class_.py.
