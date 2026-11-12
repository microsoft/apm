# Licensing

Copyright (c) Microsoft Corporation.

APM uses different licenses for software and general documentation. The scope
below applies to APM-owned material; an existing third-party license or notice
takes precedence for that material.

## Software: Apache-2.0

The [Apache License 2.0](LICENSE) applies to APM-owned software, including:

- CLI source, tests, scripts, build tooling and website implementation code.
- Bundled packages and agent primitives, including skills, prompts, instructions,
  agents, workflows and their supporting resources. Markdown used as an agent
  primitive is software, not general documentation.
- Code and configuration examples, including examples embedded in documentation.
  An example's declared package license is illustrative, not a license grant for
  the surrounding APM project.
- The software-facing OpenAPM specification in
  `docs/src/content/docs/specs/openapm-*.md`, its schemas, requirement manifests
  and conformance fixtures, including published copies.

The root `LICENSE` remains the default for APM-owned material outside the
documentation scope below, unless a file states otherwise.

## General documentation: CC-BY-4.0

The [Creative Commons Attribution 4.0 International license](LICENSE-DOCS)
applies to human-facing documentation prose, including:

- Root Markdown documents and human-facing `README.md` / `README.mdx` files.
- Guides, tutorials and explanatory reference pages under
  `docs/src/content/docs/`, and their rendered documentation-site content.

The software, examples and OpenAPM artifacts listed above are excluded from this
documentation grant, even when stored in Markdown or displayed on the same page.
This is a scope split, not a choice between two licenses for the same material.
Website dependencies, fonts and other third-party assets retain their own terms.

When sharing documentation, retain supplied attribution and notices, link to
CC-BY-4.0 and the source where reasonably practicable, and indicate changes.
For example: "Adapted from the APM documentation, Copyright (c) Microsoft
Corporation, CC-BY-4.0; changes: [describe changes]", with links to the source
page and <https://creativecommons.org/licenses/by/4.0/>.
The full license governs; this example adds no conditions. Neither license
grants permission to use trademarks or logos to imply endorsement.

## Contributions, historical releases and third-party material

Contributions use the license applicable to the material being contributed.
See [CONTRIBUTING.md](CONTRIBUTING.md) for the contribution process.

Previously published MIT releases remain under their original terms. The
historical MIT notice and existing third-party attributions are retained in
[NOTICE](NOTICE); this transition does not erase those notices or relicense
third-party dependencies.

`NOTICE` covers the direct Python runtime dependencies listed by its generator,
not every transitive, vendored, native, website or build-tool component.
Redistributors must assess the material in their actual distribution and retain
its applicable licenses and notices.
