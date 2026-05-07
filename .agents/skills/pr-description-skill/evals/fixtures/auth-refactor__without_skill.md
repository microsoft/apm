# Auth refactor

This PR refactors authentication. It centralizes token resolution
into a new AuthResolver class and removes the --token-source flag.

## What changed

- Added a new file src/apm_cli/auth/resolver.py with the
  AuthResolver class.
- Updated src/apm_cli/auth/__init__.py to export AuthResolver.
- Updated src/apm_cli/cli.py to use AuthResolver for all token
  lookups and removed the --token-source flag.
- Updated src/apm_cli/integration/git.py to delegate to
  AuthResolver and dropped the GITHUB_APM_PAT fallback.
- Added unit tests in tests/unit/auth/test_resolver.py.
- Updated CHANGELOG.md.

## Why

The previous code had token resolution logic scattered across
multiple modules. This was fragile and led to bugs. Centralizing
it makes the code cleaner and easier to maintain. This is a
significantly enhanced approach to authentication.

## Testing

I ran the tests and they passed. The audit also passed.

## Notes

This is a breaking change because the --token-source flag is
removed.
