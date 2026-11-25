"""Alias for the canonical biosim.visuals module."""
import sys
from importlib import import_module

sys.modules[__name__] = import_module("biosim.visuals")
