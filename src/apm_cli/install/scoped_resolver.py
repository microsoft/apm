"""Install-time dependency resolver that blocks ``git: parent`` at user scope."""

from ..deps.apm_resolver import APMDependencyResolver
from .package_resolution import GIT_PARENT_USER_SCOPE_ERROR


class _ScopedInstallDependencyResolver(APMDependencyResolver):
    """Install-time resolver; blocks ``git: parent`` expansion at user scope."""

    def __init__(self, *args, install_scope=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._install_scope = install_scope

    def expand_parent_repo_decl(self, parent_dep, child_dep):
        from ..core.scope import InstallScope

        if self._install_scope is InstallScope.USER:
            raise ValueError(GIT_PARENT_USER_SCOPE_ERROR)
        return super().expand_parent_repo_decl(parent_dep, child_dep)
