"""Alias for the canonical biosim.cloud module."""
import sys
from importlib import import_module

sys.modules[__name__] = import_module("biosim.cloud")
sys.modules["biosimulant.cloud.errors"] = import_module("biosim.cloud.errors")
sys.modules["biosimulant.cloud.client"] = import_module("biosim.cloud.client")
sys.modules["biosimulant.cloud.types"] = import_module("biosim.cloud.types")
sys.modules["biosimulant.cloud.webhooks"] = import_module("biosim.cloud.webhooks")
