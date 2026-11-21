"""regrag: citation-grounded retrieval and evaluation over multi-year regulations."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("regrag")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
