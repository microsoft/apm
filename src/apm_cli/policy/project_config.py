"""Project-side policy configuration helpers (closes #829).

Reads the optional top-level ``policy:`` block from the project's
``apm.yml``. Two consumer-side knobs live here:

    policy:
      fetch_failure_default: warn | block      # closes #829
      hash: "sha256:<hex>"                     # closes #827 hash pin
      hash_algorithm: sha256                   # optional, default sha256

``fetch_failure_default`` complements the org-side ``fetch_failure``
field on :class:`apm_cli.policy.schema.ApmPolicy`: both default to
``"warn"`` for backwards compatibility. When set to ``"block"``, install
fails closed if the org policy cannot be fetched / parsed (i.e. the
outcomes ``cache_miss_fetch_fail``, ``garbage_response``, ``malformed``).

``hash`` pins the SHA-256 (or other allowed digest) of the raw policy
bytes the project expects to receive. When set, a fetch that returns
different bytes -- compromised mirror, malicious intermediary, captive
portal that happens to respond with valid YAML -- is rejected fail-closed
*regardless* of ``fetch_failure_default``: a hash mismatch is an explicit
pin violation, not a fetch failure. This is the equivalent of
``pip --require-hashes`` for the policy file itself.

The org-side ``fetch_failure`` knob applies when a cached / stale policy
is available (read directly off the cached :class:`ApmPolicy`); the
project-side ``fetch_failure_default`` knob applies when no policy is
available at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional  # noqa: F401, UP035

import yaml

_VALID_FETCH_FAILURE_DEFAULT = {"warn", "block"}
_DEFAULT = "warn"

# ---------------------------------------------------------------------------
# Hash pin (closes #827 supply-chain hardening)
# ---------------------------------------------------------------------------

# Allowed hash algorithms. SHA-256 is the default; SHA-384/512 are reserved
# for regulated environments that mandate larger digests. MD5/SHA-1 are NOT
# accepted -- collision attacks are practical and the whole point of the
# pin is collision resistance.
ALLOWED_HASH_ALGORITHMS = ("sha256", "sha384", "sha512")
_DEFAULT_HASH_ALGORITHM = "sha256"
_HASH_HEX_LEN = {"sha256": 64, "sha384": 96, "sha512": 128}
_HEX_RE = re.compile(r"^[0-9a-f]+$")


class ProjectPolicyConfigError(ValueError):
    """Raised when the ``policy:`` block in apm.yml is structurally invalid.

    Used for hash-pin validation only. The ``fetch_failure_default`` reader
    is intentionally lenient (best-effort) for backwards compatibility, but
    a malformed hash pin must fail loudly -- silently ignoring it would
    defeat the security guarantee.
    """


@dataclass(frozen=True)
class ProjectPolicyHashPin:
    """Validated hash pin from ``policy.hash`` in apm.yml."""

    algorithm: str
    digest: str  # lowercase hex, no algo prefix

    @property
    def normalized(self) -> str:
        """Return the canonical ``algo:hex`` form."""
        return f"{self.algorithm}:{self.digest}"


def read_project_fetch_failure_default(project_root: Path) -> str:
    """Read ``policy.fetch_failure_default`` from ``<project_root>/apm.yml``.

    Returns ``"warn"`` (back-compat default) if:
      * apm.yml is missing
      * apm.yml is unreadable / malformed
      * the ``policy`` block or the ``fetch_failure_default`` key is absent
      * the value is not one of ``{"warn", "block"}``

    Never raises -- discovery is best-effort. A bad value is silently
    ignored (a stricter validator could surface this in ``apm audit``).
    """
    return _read_or_default(project_root, _DEFAULT)


def _read_or_default(project_root: Path, default: str) -> str:
    apm_yml = project_root / "apm.yml"
    if not apm_yml.is_file():
        return default
    try:
        raw = apm_yml.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
    except (OSError, yaml.YAMLError):
        return default
    if not isinstance(data, dict):
        return default
    policy_block = data.get("policy")
    if not isinstance(policy_block, dict):
        return default
    value: object | None = policy_block.get("fetch_failure_default")
    if isinstance(value, str) and value in _VALID_FETCH_FAILURE_DEFAULT:
        return value
    return default


# ---------------------------------------------------------------------------
# Hash pin parsing / loading
# ---------------------------------------------------------------------------


def _strip_algo_prefix(value: str, declared_algo: str) -> str:
    """Strip an optional ``<algo>:`` prefix from a pinned hash value.

    Accepts both ``"sha256:abc..."`` and bare ``"abc..."``. When a prefix
    is present it must match the declared ``hash_algorithm``.
    """
    if ":" not in value:
        return value
    algo, _, rest = value.partition(":")
    if algo.lower() != declared_algo:
        raise ProjectPolicyConfigError(
            f"policy.hash prefix '{algo}:' does not match "
            f"hash_algorithm '{declared_algo}' in apm.yml"
        )
    return rest


def parse_project_policy_hash_pin(
    raw: dict[str, Any] | None,
) -> ProjectPolicyHashPin | None:
    """Extract a :class:`ProjectPolicyHashPin` from the apm.yml policy block.

    Returns ``None`` when the block is absent, empty, or simply does not
    contain a ``hash`` key. Raises :class:`ProjectPolicyConfigError` for
    structurally invalid input -- wrong types, unsupported algorithm,
    malformed digest -- so the user finds out at parse time, not at fetch
    time when the manifest may already be in production.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ProjectPolicyConfigError("policy: block in apm.yml must be a mapping")

    algo_raw = raw.get("hash_algorithm", _DEFAULT_HASH_ALGORITHM)
    if not isinstance(algo_raw, str):
        raise ProjectPolicyConfigError("policy.hash_algorithm in apm.yml must be a string")
    algo = algo_raw.strip().lower()
    if algo not in ALLOWED_HASH_ALGORITHMS:
        allowed = ", ".join(ALLOWED_HASH_ALGORITHMS)
        raise ProjectPolicyConfigError(
            f"policy.hash_algorithm '{algo_raw}' is not supported. Allowed: {allowed}"
        )

    hash_raw = raw.get("hash")
    if hash_raw is None:
        return None
    if not isinstance(hash_raw, str):
        raise ProjectPolicyConfigError(
            "policy.hash in apm.yml must be a string of the form 'sha256:<hex>' or '<hex>'"
        )
    candidate = _strip_algo_prefix(hash_raw.strip(), algo).lower()
    expected_len = _HASH_HEX_LEN[algo]
    if len(candidate) != expected_len or not _HEX_RE.match(candidate):
        raise ProjectPolicyConfigError(
            f"policy.hash in apm.yml is not a valid {algo} digest "
            f"(expected {expected_len} lowercase hex characters)"
        )
    return ProjectPolicyHashPin(algorithm=algo, digest=candidate)


def read_project_policy_hash_pin(
    project_root: Path,
) -> ProjectPolicyHashPin | None:
    """Read ``policy.hash`` from ``<project_root>/apm.yml``.

    Returns ``None`` when the manifest is missing / unreadable / lacks a
    pin. A malformed pin raises :class:`ProjectPolicyConfigError` -- a
    silent skip would defeat the security guarantee.
    """
    apm_yml = project_root / "apm.yml"
    if not apm_yml.is_file():
        return None
    try:
        raw_text = apm_yml.read_text(encoding="utf-8")
        data = yaml.safe_load(raw_text)
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    policy_block = data.get("policy")
    if policy_block is None:
        return None
    return parse_project_policy_hash_pin(policy_block)


def compute_policy_hash(content: str, algorithm: str = _DEFAULT_HASH_ALGORITHM) -> str:
    """Compute the digest of fetched policy content under *algorithm*.

    The hash is computed on the **UTF-8 bytes of the raw policy text** --
    the same bytes that ``yaml.safe_load`` consumes -- so a malicious
    mirror cannot return semantically equivalent YAML with different bytes
    that re-serializes to the same value. ``hashlib`` from the stdlib only.
    """
    import hashlib  # local import keeps module import side-effect-free

    if algorithm not in ALLOWED_HASH_ALGORITHMS:
        raise ProjectPolicyConfigError(
            f"Refusing to compute policy hash with unsupported algorithm '{algorithm}'"
        )
    digest = hashlib.new(algorithm)
    digest.update(content.encode("utf-8"))
    return digest.hexdigest()
