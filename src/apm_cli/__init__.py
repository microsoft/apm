"""APM package."""


def __getattr__(name: str):
    # ``__version__`` resolves on first access: computing it reads installed
    # package metadata, which every ``apm_cli.*`` import would otherwise pay.
    if name == "__version__":
        from .version import get_version

        value = get_version()
        globals()["__version__"] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
