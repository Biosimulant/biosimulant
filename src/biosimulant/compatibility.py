"""Alias for the canonical biosim.compatibility module."""
import sys
from importlib import import_module

sys.modules[__name__] = import_module("biosim.compatibility")
