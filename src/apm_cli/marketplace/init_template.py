"""Template renderer for ``apm marketplace init``.

Produces a richly-commented ``marketplace.yml`` scaffold that is valid
against :func:`~apm_cli.marketplace.yml_schema.load_marketplace_yml`.
"""

from __future__ import annotations

# The template uses Python str.format() with named placeholders for
# {name} and {owner}.  Literal braces (e.g. in tagPattern) are doubled.

_TEMPLATE = """\
# APM marketplace descriptor
#
# This file (marketplace.yml) is the SOURCE for your marketplace.
# Run 'apm marketplace build' to compile it to marketplace.json.
# Both files must be committed to the repository.
#
# For the full schema, see:
#   https://microsoft.github.io/apm/guides/marketplace-authoring/

name: {name}
description: A short description of what your marketplace offers

# Semantic version of this marketplace (bump on release)
version: 0.1.0

owner:
  name: {owner}
  url: https://github.com/{owner}
  # email: maintainers@{owner}.example       # optional

# APM-only build options (stripped from compiled marketplace.json)
build:
  # Default tag pattern used to resolve {{version}} for each package.
  # Supports {{name}} and {{version}} placeholders. Override per-package below.
  tagPattern: "v{{version}}"

# Opaque pass-through metadata (copied verbatim to marketplace.json).
# Use this for Anthropic-recognised or marketplace-specific fields.
metadata:
  # Example: maintained by {owner}
  homepage: https://example.com

packages:
  - name: example-package
    description: Human-readable description of the package
    source: {owner}/example-package
    version: "^1.0.0"
    # Optional overrides:
    # subdir: path/inside/repo
    # tagPattern: "example-package-v{{version}}"
    # include_prerelease: false
    # ref: abcdef1234  # pin to explicit SHA/tag/branch (overrides version range)

  # Alternative: pin a package to an explicit branch or SHA instead of a
  # version range.  Uncomment the entry below and remove the 'version' line.
  #
  # - name: pinned-package
  #   description: Pinned to a specific commit
  #   source: {owner}/pinned-package
  #   ref: main
"""


def render_marketplace_yml_template(
    name: str | None = None,
    owner: str | None = None,
) -> str:
    """Return the scaffold content for a new ``marketplace.yml``.

    Parameters
    ----------
    name:
        Marketplace name. Defaults to ``my-marketplace``.
    owner:
        Owner name. Defaults to ``acme-org``.
    """
    return _TEMPLATE.format(
        name=name or "my-marketplace",
        owner=owner or "acme-org",
    )
