"""Template renderer for ``apm marketplace init``.

Produces a richly-commented ``marketplace.yml`` scaffold that is valid
against :func:`~apm_cli.marketplace.yml_schema.load_marketplace_yml`.
"""

from __future__ import annotations

# The template is a plain string literal so it can be returned verbatim
# without runtime formatting.  Every line is pure ASCII.

_TEMPLATE = """\
# APM marketplace descriptor
#
# This file (marketplace.yml) is the SOURCE for your marketplace.
# Run 'apm marketplace build' to compile it to marketplace.json.
# Both files must be committed to the repository.
#
# For the full schema, see:
#   https://microsoft.github.io/apm/guides/marketplace-authoring/

name: my-marketplace
description: A short description of what your marketplace offers

# Semantic version of this marketplace (bump on release)
version: 0.1.0

owner:
  name: acme-org
  url: https://github.com/acme-org
  # email: maintainers@acme-org.example       # optional

# APM-only build options (stripped from compiled marketplace.json)
build:
  # Default tag pattern used to resolve {version} for each package.
  # Supports {name} and {version} placeholders. Override per-package below.
  tagPattern: "v{version}"

# Opaque pass-through metadata (copied verbatim to marketplace.json).
# Use this for Anthropic-recognised or marketplace-specific fields.
metadata:
  # Example: maintained by acme-org
  homepage: https://example.com

packages:
  - name: example-package
    description: Human-readable description of the package
    source: acme-org/example-package
    version: "^1.0.0"
    # Optional overrides:
    # subdir: path/inside/repo
    # tagPattern: "example-package-v{version}"
    # includePrerelease: false
    # ref: abcdef1234  # pin to explicit SHA/tag/branch (overrides version range)

  # Alternative: pin a package to an explicit branch or SHA instead of a
  # version range.  Uncomment the entry below and remove the 'version' line.
  #
  # - name: pinned-package
  #   description: Pinned to a specific commit
  #   source: acme-org/pinned-package
  #   ref: main
"""


def render_marketplace_yml_template() -> str:
    """Return the scaffold content for a new ``marketplace.yml``."""
    return _TEMPLATE
