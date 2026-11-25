"""Alias for the canonical biosim.execution_outcome module."""
import sys
from importlib import import_module

sys.modules[__name__] = import_module("biosim.execution_outcome")
