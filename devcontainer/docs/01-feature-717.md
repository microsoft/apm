**Is your feature request related to a problem? Please describe.**  
APM is not currently available as a Dev Container Feature, creating one will allow for easier consumption of the tool.

The only option today is to manually install it in `devcontainer.json` (e.g. via `postCreateCommand`), which is less reusable, less discoverable, and harder to standardise across environments. It also prevents APM from appearing in the feature ecosystem (https://containers.dev/features).

**Describe the solution you'd like**  
Provide APM as a Dev Container Feature.

This would allow usage like:

```json
{
  "features": {
    "ghcr.io/devcontainers/features/apm:1": {}
  }
}
```

or, if published under a Microsoft-owned collection:

```json
{
  "features": {
    "ghcr.io/microsoft/devcontainer-features/apm:1": {}
  }
}
```

Implementation would require the steps noted [here](https://containers.dev/implementors/features/).

**Describe alternatives you've considered**

- Manual install in `devcontainer.json` (e.g. `postCreateCommand`)
  - Works, but is not reusable or discoverable and must be repeated across projects

- Publishing via a separate non-Microsoft collection
  - Creates ambiguity around ownership and long-term maintenance

**Additional context**  
There are two plausible locations for this:

- [devcontainers/features/src/apm](https://github.com/devcontainers/features/tree/main/src) (if maintainers accept a new feature)
- A Microsoft-owned Dev Container Feature collection (if ownership is preferred) - e.g. `microsoft/devcontainer-features`
