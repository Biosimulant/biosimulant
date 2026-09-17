"""Exception types shared by modules that cannot import each other."""

from __future__ import annotations


class PackageError(ValueError):
    """Raised when a package is invalid or cannot be handled."""


PackageError.__module__ = "biosim.pack"
