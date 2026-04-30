"""MCP-specific helpers for the ``apm install --mcp`` code path.

Modules in this subpackage compose the user-visible MCP install flow
(parse args -> build entry -> warn -> write apm.yml -> integrate). They
sit alongside the main install pipeline (``install/pipeline.py`` and
``install/phases/``) but form an independent flow that the
``commands/install.py`` Click handler delegates to when ``--mcp`` is set.

Layout:

    args.py         CLI ``--env`` / ``--header`` KEY=VAL parsers
    command.py      ``run_mcp_install`` orchestrator
    conflicts.py    ``--mcp`` flag conflict matrix (E1-E15)
    entry.py        Pure ``apm.yml`` MCP entry builder (str | dict union)
    registry.py     ``--registry`` validation, precedence, env override
    warnings.py     F5 SSRF + F7 shell-metachar install-time warnings
    writer.py       Idempotent ``apm.yml`` MCP entry persistence
"""
