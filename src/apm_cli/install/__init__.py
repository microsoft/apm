"""APM install engine.

This package implements the install pipeline that the
`apm_cli.commands.install` Click command delegates to.

Architecture:

    pipeline.py     orchestrator that calls each phase in order
    context.py      InstallContext dataclass (state passed between phases)
    request.py      InstallRequest dataclass (typed CLI inputs)
    service.py      InstallService Application Service (entry point)
    services.py     DI seam re-exporting integration helpers
    sources.py      DependencySource Strategy hierarchy
    template.py     run_integration_template() Template Method
    validation.py   manifest validation (dependency syntax, existence checks)

    phases/         one module per pipeline phase
    helpers/        cross-cutting helpers (security scan, gitignore)
    presentation/   dry-run preview + final result rendering
    mcp/            ``apm install --mcp`` flow (parse / build / warn / write)

The engine is import-safe (no Click decorators at top level) so phase modules
can be unit-tested directly without invoking the CLI.
"""
