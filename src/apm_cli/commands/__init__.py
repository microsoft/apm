"""Commands package for APM CLI.

Command modules load lazily: ``apm_cli.cli`` dispatches verbs by module path,
so importing one verb (for example ``apm_cli.commands.install``) must not pay
for another verb's import graph.
"""

__all__ = ["deps"]


def __getattr__(name: str):
    """Resolve ``apm_cli.commands.deps`` (the Click group) on first access."""
    if name == "deps":
        from .deps import deps

        globals()["deps"] = deps
        return deps
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
