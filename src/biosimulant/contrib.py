"""Alias for the canonical biosim.contrib module."""
import sys
from importlib import import_module

sys.modules[__name__] = import_module("biosim.contrib")
sys.modules["biosimulant.contrib.cellml"] = import_module("biosim.contrib.cellml")
sys.modules["biosimulant.contrib.sbml"] = import_module("biosim.contrib.sbml")
