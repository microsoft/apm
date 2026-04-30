"""Template renderers for marketplace authoring scaffolds.

Two renderers ship in this module:

* :func:`render_marketplace_yml_template` -- legacy ``marketplace.yml``
  scaffold, retained for one release while the deprecation runs out.
* :func:`render_marketplace_block` -- the apm.yml ``marketplace:`` block
  used by ``apm marketplace init`` and ``apm init --marketplace``.
"""

from __future__ import annotations

# The template uses Python str.format() with named placeholders for
# {name} and {owner}.  Literal braces (e.g. in tagPattern) are doubled.

_TEMPLATE = """\
# APM marketplace descriptor
#
# This file (marketplace.yml) is the SOURCE for your marketplace.
# Run 'apm pack' to compile it to marketplace.json.
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


_MARKETPLACE_BLOCK_TEMPLATE = """\
# Marketplace authoring config (APM-only).
# Run 'apm pack' to compile this block to .claude-plugin/marketplace.json.
#
# Top-level 'name', 'description', and 'version' are inherited from
# the project (above) by default.  Override them inside this block when
# the marketplace is published independently of the project's release
# cadence.
#
# For the full schema, see:
#   https://microsoft.github.io/apm/guides/marketplace-authoring/
marketplace:
  owner:
    name: {owner}
    url: https://github.com/{owner}

  # Default tag pattern used to resolve version ranges for each package.
  build:
    tagPattern: "v{{version}}"

  packages:
    - name: example-package
      description: Human-readable description of the package
      source: {owner}/example-package
      version: "^1.0.0"
      # Optional overrides:
      # subdir: path/inside/repo
      # tagPattern: "example-package-v{{version}}"
      # include_prerelease: false
      # ref: main  # pin to an explicit ref instead of a version range

    # Local-path entry: ship a package shipped alongside this repo.
    # - name: local-tool
    #   source: ./packages/local-tool
    #   description: A locally vendored tool
    #   version: 0.1.0
"""


def render_marketplace_block(owner: str | None = None) -> str:
    """Return a YAML snippet for the ``marketplace:`` block of apm.yml.

    Used by ``apm init --marketplace`` to seed a new project that ships
    its own marketplace.  ``name``/``description``/``version`` are
    inherited from the surrounding apm.yml top level by default, so they
    are intentionally omitted here.
    """
    return _MARKETPLACE_BLOCK_TEMPLATE.format(
        owner=owner or "acme-org",
    )
