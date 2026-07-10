# Improve install path security

## Summary

We reviewed the install path and found that a third-party token was
being printed to stdout. To address this we added a secret scanner
module that inspects all output for token-shaped strings across AWS,
GitHub, and Azure formats, and redacts them.

## Changes

- New scanner module that detects leaked secrets by regex shape.
- Wired the scanner into the install output path.
- Added 1,400 lines of detector tests across provider formats.

This significantly enhances the security posture of the installer.
