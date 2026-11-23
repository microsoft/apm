"""APM package."""

from .utils import path_fast as _path_fast  # noqa: F401  RESEARCH ONLY
from .version import get_version

__version__ = get_version()
