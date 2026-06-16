"""SBOM format identifiers (issue #1777, U5).

Kept in a dependency-free module so the CLI can build the ``apm lock export
--format`` choice without importing the SBOM serializer -- and, transitively,
the bundled SPDX identifier table. Importing this module costs nothing; the
heavy ``spdx_data`` table is only loaded when an export actually runs.
"""

from __future__ import annotations

FORMAT_CYCLONEDX = "cyclonedx"
FORMAT_SPDX = "spdx"
SUPPORTED_FORMATS = (FORMAT_CYCLONEDX, FORMAT_SPDX)

__all__ = ["FORMAT_CYCLONEDX", "FORMAT_SPDX", "SUPPORTED_FORMATS"]
