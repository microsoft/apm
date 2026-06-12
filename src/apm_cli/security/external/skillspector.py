"""NVIDIA SkillSpector adapter.

Invokes the SkillSpector CLI -- when it is resolvable on ``PATH`` -- over the
requested paths, asks it for SARIF output, and folds the findings into the
audit pipeline.  The vendor CLI is located lazily via ``shutil.which`` and
never imported as a Python package, so this adapter works identically whether
APM runs from source or as the self-contained PyInstaller binary.

Users who cannot install the SkillSpector CLI can instead emit a SARIF file
from any tool and ingest it with ``--external sarif --external-sarif <file>``.

APM only consumes SkillSpector's SARIF; it publishes nothing back
(one-directional, no partnership framing).

By default SkillSpector runs offline (``--no-llm``): deterministic, no network
egress, no credentials.  Opting into LLM-powered analysis (``options.llm``)
makes outbound API calls and consumes an API key from the environment; that key
is only forwarded to the subprocess when LLM mode is active (env minimisation),
and any captured stderr is secret-redacted before it reaches an error message.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from ..content_scanner import ScanFinding
from .base import ExternalScanError

if TYPE_CHECKING:
    from .options import ScannerOptions

#: Executable name expected on PATH when the SkillSpector CLI is installed.
_BINARY = "skillspector"

#: Bounded wall-clock budget so a hung vendor process can't stall the audit.
_TIMEOUT_SECONDS = 300

#: Environment variables SkillSpector reads for LLM-powered analysis. Only
#: forwarded to the subprocess when LLM mode is active; never logged.
_LLM_KEY_ENV_VARS = ("OPENAI_API_KEY", "NVIDIA_INFERENCE_KEY")


class SkillSpectorAdapter:
    """Run NVIDIA SkillSpector and ingest its SARIF output."""

    name = "skillspector"

    #: Flag names a user may pass through via ``--external-args`` /
    #: ``external.skillspector.args``. Deliberately narrow: only tuning knobs
    #: that neither write files, load external rulesets/code, nor carry
    #: credentials. Everything else is rejected fail-closed (see
    #: :func:`options.validate_extra_args`).
    ALLOWED_ARG_PREFIXES = frozenset(
        {
            "--model",
            "--severity",
            "--severity-threshold",
            "--threshold",
            "--profile",
            "--lang",
            "--language",
            "--exclude",
            "--include",
        }
    )

    def is_available(self, *, options: ScannerOptions | None = None) -> tuple[bool, str | None]:
        """Available iff the binary is on PATH (and, under LLM, a key is set)."""
        if shutil.which(_BINARY) is None:
            return (
                False,
                "SkillSpector CLI not found on PATH.\n\n"
                "To install it (requires Python >= 3.12):\n"
                '  uv tool install "skillspector @ '
                'git+https://github.com/NVIDIA/SkillSpector.git"\n'
                '  pip install "skillspector @ '
                'git+https://github.com/NVIDIA/SkillSpector.git"\n\n'
                "After install, verify with: skillspector --version\n\n"
                "Alternatively, generate SARIF from any scanner and ingest it:\n"
                "  apm audit --external sarif --external-sarif report.sarif",
            )
        if options is not None and options.llm:
            if not any(os.environ.get(var) for var in _LLM_KEY_ENV_VARS):
                names = " or ".join(_LLM_KEY_ENV_VARS)
                return (
                    False,
                    f"LLM analysis for 'skillspector' requires an API key. Set "
                    f"{names}, or drop --external-llm to run offline (--no-external-llm).",
                )
        return True, None

    def scan(
        self, paths: list[Path], *, options: ScannerOptions | None = None
    ) -> dict[str, list[ScanFinding]]:
        """Invoke SkillSpector over *paths* and parse its SARIF output."""
        from .options import ScannerOptions, validate_extra_args
        from .sarif_ingest import sarif_to_findings

        if options is None:
            options = ScannerOptions()

        binary = shutil.which(_BINARY)
        if binary is None:
            raise ExternalScanError(
                "SkillSpector CLI not found on PATH.\n\n"
                "To install it (requires Python >= 3.12):\n"
                '  uv tool install "skillspector @ '
                'git+https://github.com/NVIDIA/SkillSpector.git"\n'
                '  pip install "skillspector @ '
                'git+https://github.com/NVIDIA/SkillSpector.git"\n\n'
                "After install, verify with: skillspector --version\n\n"
                "Alternatively, generate SARIF from any scanner and ingest it:\n"
                "  apm audit --external sarif --external-sarif report.sarif"
            )

        extra_args = validate_extra_args(
            self.name,
            options.extra_args,
            self.ALLOWED_ARG_PREFIXES,
            base_dir=Path.cwd(),
        )

        targets = [str(p) for p in paths] or ["."]
        cmd = [binary, "scan", "--format", "sarif"]
        if not options.llm:
            cmd.append("--no-llm")
        cmd.extend(extra_args)
        cmd.extend(targets)

        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_SECONDS,
                check=False,
                env=self._subprocess_env(options.llm),
            )
        except subprocess.TimeoutExpired as exc:
            raise ExternalScanError(f"SkillSpector timed out after {_TIMEOUT_SECONDS}s.") from exc
        except OSError as exc:
            raise ExternalScanError(f"Could not launch SkillSpector: {exc}") from exc

        if not completed.stdout.strip():
            # Non-zero exit with no SARIF is a tool error, not findings.
            detail = self._redact(completed.stderr) or f"exit code {completed.returncode}"
            raise ExternalScanError(f"SkillSpector produced no SARIF output ({detail}).")

        try:
            document = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            # SkillSpector writes errors (e.g. missing API key) to stdout,
            # not stderr.  Surface the first line so users can diagnose.
            # Strip non-printable / non-ASCII chars to honour the repo's
            # printable-ASCII output contract.
            raw_line = completed.stdout.strip().splitlines()[0][:200]
            safe_line = "".join(ch if 0x20 <= ord(ch) <= 0x7E else "?" for ch in raw_line)
            raise ExternalScanError(
                f"SkillSpector output is not valid JSON SARIF: {safe_line}"
            ) from exc

        return sarif_to_findings(document, tool_name=self.name)

    @staticmethod
    def _subprocess_env(llm: bool | None) -> dict[str, str]:
        """Build the subprocess environment with LLM keys minimised.

        When LLM mode is off, the API-key variables are stripped so an offline
        scan never exposes credentials to the vendor process.
        """
        env = dict(os.environ)
        if not llm:
            for var in _LLM_KEY_ENV_VARS:
                env.pop(var, None)
        return env

    @staticmethod
    def _redact(text: str | None) -> str:
        """Secret-redact captured stderr before it reaches a user message."""
        from ...core.plugin_manifest import _redact_secret_values

        redacted, _ = _redact_secret_values((text or "").strip())
        return redacted
