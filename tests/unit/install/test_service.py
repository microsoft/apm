"""Direct tests for the InstallService Application Service.

These tests bypass Click entirely -- they construct an InstallRequest
and call ``InstallService.run()`` directly.  This is the contract that
future programmatic / API callers will depend on.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from apm_cli.install.request import InstallRequest
from apm_cli.install.service import InstallService


@pytest.fixture
def fake_apm_package():
    pkg = MagicMock()
    pkg.dependencies = {"apm": []}
    return pkg


def _make_request(pkg, **overrides):
    base = dict(
        apm_package=pkg,
        update_refs=False,
        verbose=False,
        only_packages=None,
        force=False,
        parallel_downloads=4,
        logger=None,
        scope=None,
        auth_resolver=None,
        target=None,
        allow_insecure=False,
        allow_insecure_hosts=(),
        marketplace_provenance=None,
    )
    base.update(overrides)
    return InstallRequest(**base)


class TestInstallRequest:
    def test_request_is_frozen(self, fake_apm_package):
        from dataclasses import FrozenInstanceError

        request = _make_request(fake_apm_package)
        with pytest.raises(FrozenInstanceError):
            request.force = True

    def test_request_defaults(self, fake_apm_package):
        request = InstallRequest(apm_package=fake_apm_package)
        assert request.update_refs is False
        assert request.parallel_downloads == 4
        assert request.only_packages is None
        assert request.target is None
        assert request.allow_insecure is False
        assert request.allow_insecure_hosts == ()

    def test_only_packages_is_shallow_immutable(self, fake_apm_package):
        # Documents the known limitation: frozen=True locks the
        # InstallRequest fields themselves, but the list reference is
        # still mutable.  Future hardening could swap to a tuple.
        request = _make_request(fake_apm_package, only_packages=["pkg-a"])
        request.only_packages.append("pkg-b")
        assert request.only_packages == ["pkg-a", "pkg-b"]


class TestInstallServiceDelegation:
    def test_run_delegates_to_pipeline_with_request_fields(self, fake_apm_package):
        request = _make_request(
            fake_apm_package,
            update_refs=True,
            verbose=True,
            force=True,
            parallel_downloads=8,
            target="copilot",
            allow_insecure=True,
            allow_insecure_hosts=("mirror.example.com",),
            only_packages=["alpha", "beta"],
            marketplace_provenance={"source": "test-marketplace"},
        )
        with patch("apm_cli.install.pipeline.run_install_pipeline") as mock_run:
            mock_run.return_value = "result-sentinel"
            result = InstallService().run(request)

        assert result == "result-sentinel"
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert args[0] is fake_apm_package
        assert kwargs["update_refs"] is True
        assert kwargs["verbose"] is True
        assert kwargs["force"] is True
        assert kwargs["parallel_downloads"] == 8
        assert kwargs["target"] == "copilot"
        assert kwargs["allow_insecure"] is True
        assert kwargs["allow_insecure_hosts"] == ("mirror.example.com",)
        assert kwargs["only_packages"] == ["alpha", "beta"]
        assert kwargs["marketplace_provenance"] == {"source": "test-marketplace"}

    def test_run_passes_optional_collaborators(self, fake_apm_package):
        logger = MagicMock()
        auth = MagicMock()
        scope = MagicMock()
        request = _make_request(fake_apm_package, logger=logger, auth_resolver=auth, scope=scope)
        with patch("apm_cli.install.pipeline.run_install_pipeline") as mock_run:
            InstallService().run(request)

        kwargs = mock_run.call_args.kwargs
        assert kwargs["logger"] is logger
        assert kwargs["auth_resolver"] is auth
        assert kwargs["scope"] is scope

    def test_service_is_reusable_across_invocations(self, fake_apm_package):
        service = InstallService()
        with patch("apm_cli.install.pipeline.run_install_pipeline") as mock_run:
            mock_run.return_value = "ok"
            service.run(_make_request(fake_apm_package))
            service.run(_make_request(fake_apm_package, force=True))
        assert mock_run.call_count == 2


class TestClickWrapperUsesService:
    def test_install_apm_dependencies_builds_request_and_uses_service(self, fake_apm_package):
        from apm_cli.commands import install as install_mod

        with patch("apm_cli.install.service.InstallService.run") as mock_run:
            mock_run.return_value = "wrapped-result"
            result = install_mod._install_apm_dependencies(
                fake_apm_package,
                update_refs=True,
                force=True,
                parallel_downloads=2,
                target="claude",
                allow_insecure=True,
                allow_insecure_hosts=("mirror.example.com",),
            )

        assert result == "wrapped-result"
        mock_run.assert_called_once()
        request = mock_run.call_args.args[0]
        assert isinstance(request, InstallRequest)
        assert request.apm_package is fake_apm_package
        assert request.update_refs is True
        assert request.force is True
        assert request.parallel_downloads == 2
        assert request.target == "claude"
        assert request.allow_insecure is True
        assert request.allow_insecure_hosts == ("mirror.example.com",)
