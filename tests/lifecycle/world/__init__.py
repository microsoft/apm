"""Shared hermetic world of the lifecycle test family (#4094).

``build_template`` builds one world per pytest worker (packages, git
repositories, local services); ``clone`` gives each test a private
copy-on-write copy in milliseconds. ``fixtures.py`` exposes them as the
``lifecycle_template`` and ``lifecycle_world`` fixtures.
"""

from __future__ import annotations

from tests.lifecycle.world import registry
from tests.lifecycle.world.builder import build_template
from tests.lifecycle.world.clone import clone
from tests.lifecycle.world.contract import PackageRef, Services, World
from tests.lifecycle.world.scopes import (
    apm_home_outside_home,
    login_azure,
    use_ado_pat,
    use_policy,
    use_registry,
    use_registry_proxy,
)

__all__ = [
    "PackageRef",
    "Services",
    "World",
    "apm_home_outside_home",
    "build_template",
    "clone",
    "login_azure",
    "registry",
    "use_ado_pat",
    "use_policy",
    "use_registry",
    "use_registry_proxy",
]
