"""Alias for the canonical biosim.visual_contract module."""
import sys
from importlib import import_module

sys.modules[__name__] = import_module("biosim.visual_contract")
