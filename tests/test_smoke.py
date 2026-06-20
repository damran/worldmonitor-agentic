"""Smoke test: the package imports and exposes a version."""

from worldmonitor import __version__


def test_version_is_nonempty() -> None:
    assert isinstance(__version__, str)
    assert __version__
