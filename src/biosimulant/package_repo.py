"""Alias for the canonical biosim.package_repo module."""
import sys
from importlib import import_module

sys.modules[__name__] = import_module("biosim.package_repo")
