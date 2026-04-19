"""APM install engine.

This package implements the install pipeline that the
`apm_cli.commands.install` Click command delegates to.

Architecture (in progress; see refactor/install-modularization branch):

    pipeline.py     orchestrator that calls each phase in order
    context.py      InstallContext dataclass (state passed between phases)
    options.py      InstallOptions dataclass (parsed CLI options)
    validation.py   manifest validation (dependency syntax, existence checks)

    phases/         one module per pipeline phase
    helpers/        cross-cutting helpers (security scan, gitignore)
    presentation/   dry-run preview + final result rendering

The engine is import-safe (no Click decorators at top level) so phase modules
can be unit-tested directly without invoking the CLI.
"""
