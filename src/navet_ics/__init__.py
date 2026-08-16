"""Navet calendar proxy.

The version is read from the installed package metadata rather than restated
here, so `pyproject.toml` stays the only place it is written down and the number
shown in `/docs` cannot drift from the one that was deployed.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("navet-calendar-proxy")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0+unknown"

__all__ = ["__version__"]
