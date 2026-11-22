"""Bundle creation and consumption for APM packages."""

# PEP 562 lazy re-exports: install resolves local bundles through
# ``apm_cli.bundle.local_bundle`` and must not load the packer, unpacker and
# plugin exporter graphs just by importing a sibling submodule.
_LAZY_EXPORTS = {
    "PackResult": ".packer",
    "pack_bundle": ".packer",
    "export_plugin_bundle": ".plugin_exporter",
    "UnpackResult": ".unpacker",
    "unpack_bundle": ".unpacker",
}

__all__ = [
    "PackResult",
    "UnpackResult",
    "export_plugin_bundle",
    "pack_bundle",
    "unpack_bundle",
]


def __getattr__(name: str):
    module = _LAZY_EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
