"""SBOM / provenance export for APM.

This package serializes an existing ``apm.lock.yaml`` into a software
bill-of-materials (SBOM). It is an **inventory export**, not a security
attestation: every value is read from the lockfile that a prior resolve
already recorded. Export never re-hashes, re-resolves, reaches the network,
or scans license text. It records WHAT reached disk and its declared
provenance; it concludes nothing.
"""

from __future__ import annotations

__all__: list[str] = []
