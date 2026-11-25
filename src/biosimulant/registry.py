"""Alias for the canonical biosim.registry module."""
import sys
from importlib import import_module

sys.modules[__name__] = import_module("biosim.registry")
