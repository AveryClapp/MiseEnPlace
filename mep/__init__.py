"""mep — extract recipes from YouTube cooking videos into local SQLite."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("mise-en-place")
except PackageNotFoundError:  # not installed (e.g. running from a source checkout)
    __version__ = "0.0.0+unknown"
