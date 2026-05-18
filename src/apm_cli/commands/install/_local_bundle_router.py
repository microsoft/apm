"""Local-bundle early-exit routing for the install command.

Extracts the filesystem-path detection + dispatch logic that was inline in
``install_impl.install()``.  Kept in a sibling private module to stay within
the ≤500-line budget for ``install_impl.py`` (issue #1076).
"""

from __future__ import annotations

from pathlib import Path

import click


def _route_local_bundle_or_raise(**kwargs) -> bool:
    """Handle the local-bundle early-exit path inside ``install()``.

    Returns ``True`` when a bundle was detected *and* installed — the caller
    should set ``summary_rendered = True`` and ``return`` immediately.

    Returns ``False`` when no local-bundle path was detected so the caller
    continues with normal dependency-resolution.

    Raises :class:`click.UsageError` for:
    - an unrecognised ``.tar.gz`` / ``.tgz`` file (IM7), or
    - ``--as`` supplied without a valid local-bundle path (IM8).
    """
    packages = kwargs["packages"]
    mcp_name = kwargs["mcp_name"]
    target = kwargs["target"]
    global_ = kwargs["global_"]
    force = kwargs["force"]
    dry_run = kwargs["dry_run"]
    verbose = kwargs["verbose"]
    alias = kwargs["alias"]
    logger = kwargs["logger"]
    legacy_skill_paths = kwargs["legacy_skill_paths"]
    update = kwargs["update"]
    only = kwargs["only"]
    runtime = kwargs["runtime"]
    exclude = kwargs["exclude"]
    dev = kwargs["dev"]
    use_ssh = kwargs["use_ssh"]
    use_https = kwargs["use_https"]
    allow_protocol_fallback = kwargs["allow_protocol_fallback"]
    registry_url = kwargs["registry_url"]
    skill_names = kwargs["skill_names"]
    parallel_downloads = kwargs["parallel_downloads"]
    allow_insecure = kwargs["allow_insecure"]
    allow_insecure_hosts = kwargs["allow_insecure_hosts"]
    no_policy = kwargs["no_policy"]
    # ----------------------------------------------------------------
    # Local-bundle early-exit (issue #1098).  When the sole positional
    # argument is a filesystem path that detect_local_bundle() recognises
    # as an APM-pack bundle, we skip the dependency-resolution pipeline
    # entirely and deploy the bundle's files directly.  Local bundles
    # are imperative deploys -- they do NOT mutate apm.yml.
    # ----------------------------------------------------------------
    if len(packages) == 1 and not mcp_name and (_probe := Path(packages[0])).exists():
        from ...bundle.local_bundle import detect_local_bundle as _detect_lb
        from ...install.local_bundle_handler import install_local_bundle as _install_lb

        _bundle_info = _detect_lb(_probe)
        if _bundle_info is not None:
            from ...install.local_bundle_handler import _InstallFlags as _IFlags

            _install_lb(
                bundle_info=_bundle_info,
                bundle_arg=packages[0],
                target=target,
                global_=global_,
                install_flags=_IFlags(force=force, dry_run=dry_run, logger=logger),
                verbose=verbose,
                alias=alias,
                legacy_skill_paths=legacy_skill_paths,
                # Rejected-flag context for consolidated UsageError:
                rejected_flags={
                    "--update": update,
                    "--only": only,
                    "--runtime": runtime,
                    "--exclude": exclude,
                    "--dev": dev,
                    "--ssh": use_ssh,
                    "--https": use_https,
                    "--allow-protocol-fallback": allow_protocol_fallback,
                    "--mcp": mcp_name,
                    "--registry": registry_url,
                    "--skill": bool(skill_names),
                    "--parallel-downloads": parallel_downloads != 4,
                    "--allow-insecure": allow_insecure,
                    "--allow-insecure-host": bool(allow_insecure_hosts),
                    "--no-policy": no_policy,
                },
            )
            # Local bundle install renders its own summary; returning True
            # signals the caller to set summary_rendered = True and return.
            # See issue #1207 D3.
            return True

        # IM7: path exists but isn't a recognised bundle.  For tarball
        # extensions (.tar.gz / .tgz) the user clearly meant a bundle
        # artifact, so raise a targeted UsageError instead of falling
        # through to the registry path (which would try to clone).
        # For bare directories we still fall through, because
        # ``apm install ./packages/source-pkg`` is a supported local-path
        # install that goes through the dependency-resolver pipeline.
        _suffix = _probe.name.lower()
        if _probe.is_file() and (_suffix.endswith(".tar.gz") or _suffix.endswith(".tgz")):
            # Distinguish legacy --format apm bundles (apm.lock.yaml
            # present, plugin.json absent) from arbitrary tarballs so
            # the error message guides the user to the right next step.
            from ...bundle.local_bundle import _looks_like_legacy_apm_bundle

            if _looks_like_legacy_apm_bundle(_probe):
                raise click.UsageError(
                    f"'{packages[0]}' was packed with '--format apm' (legacy format). "
                    "'apm install <bundle>' requires the plugin format. "
                    "Repack with 'apm pack --format plugin --archive', "
                    "or use 'apm unpack' to deploy the legacy bundle."
                )
            raise click.UsageError(
                f"'{packages[0]}' is not a valid APM bundle archive "
                "(no plugin.json found at the bundle root). "
                "Use 'apm install org/package' for registry installs, "
                "or repack the source with 'apm pack'."
            )

    # IM8: --as is only meaningful for local-bundle installs.  If we get
    # here, no local bundle was detected, so reject --as instead of
    # silently ignoring it.
    if alias:
        raise click.UsageError(
            "--as requires a local bundle path (directory or .tar.gz "
            "produced by 'apm pack'). It has no effect on registry installs."
        )

    return False
