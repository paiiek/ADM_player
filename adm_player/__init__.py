"""ADM BWF playback with ADM-OSC metadata streaming."""

try:
    from importlib.metadata import PackageNotFoundError, version as _pkg_version

    try:
        __version__ = _pkg_version("adm-player")
    except PackageNotFoundError:  # not installed (running from a source checkout)
        __version__ = "0.0.0+local"
except ImportError:  # pragma: no cover — importlib.metadata is stdlib on 3.10+
    __version__ = "0.0.0+local"
