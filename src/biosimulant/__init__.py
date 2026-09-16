# SPDX-FileCopyrightText: 2026-present Biosimulant Team
#
# SPDX-License-Identifier: MIT
"""Primary Biosimulant Python namespace.

The implementation currently lives in :mod:`biosim` for compatibility with
existing model packages. This module intentionally delegates instead of copying
runtime APIs so both import paths share one implementation owner.
"""
from __future__ import annotations

import importlib
import sys
from typing import Any

import biosim as _biosim
from biosim import *  # noqa: F401,F403
from biosim import __all__ as __all__
from biosim import __version__ as __version__


def __getattr__(name: str) -> Any:
    return getattr(_biosim, name)


def __dir__() -> list[str]:
    return sorted([*__all__, "onnx"])


_ALIASED_SUBMODULES = (
    "compatibility",
    "contrib",
    "cloud",
    "contrib.cellml",
    "contrib.sbml",
    "credentials",
    "hub",
    "modules",
    "onnx",
    "pack",
    "package_repo",
    "runtime",
    "runtime.coercion",
    "runtime.entrypoint",
    "runtime.flatten",
    "runtime.runtime_config",
    "runtime.types",
    "registry",
    "signals",
    "visuals",
    "wiring",
    "world",
)

for _name in _ALIASED_SUBMODULES:
    sys.modules[f"{__name__}.{_name}"] = importlib.import_module(f"biosim.{_name}")

del _name
