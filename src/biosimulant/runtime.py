"""Alias for the canonical biosim.runtime module."""
import sys
from importlib import import_module

sys.modules[__name__] = import_module("biosim.runtime")
sys.modules["biosimulant.runtime.coercion"] = import_module("biosim.runtime.coercion")
sys.modules["biosimulant.runtime.entrypoint"] = import_module("biosim.runtime.entrypoint")
sys.modules["biosimulant.runtime.flatten"] = import_module("biosim.runtime.flatten")
sys.modules["biosimulant.runtime.runtime_config"] = import_module("biosim.runtime.runtime_config")
sys.modules["biosimulant.runtime.types"] = import_module("biosim.runtime.types")
