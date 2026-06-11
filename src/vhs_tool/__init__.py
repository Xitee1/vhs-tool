"""vhs-tool — unified CLI for the VHS decode pipeline."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("vhs-tool")
except PackageNotFoundError:  # running from source without install
    __version__ = "0.0.0+unknown"
