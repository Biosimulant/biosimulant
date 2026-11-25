# SPDX-FileCopyrightText: 2026-present Biosimulant Team
#
# SPDX-License-Identifier: MIT
"""Primary Biosimulant Python namespace.

The implementation currently lives in :mod:`biosim` for compatibility with
existing model packages. This module intentionally delegates instead of copying
runtime APIs so both import paths share one implementation owner.
"""
from __future__ import annotations

from pkgutil import extend_path
from typing import Any

import biosim as _biosim

__path__ = extend_path(__path__, __name__)
from biosim import __all__ as __all__
from biosim import __version__ as __version__


def __getattr__(name: str) -> Any:
    return getattr(_biosim, name)


def __dir__() -> list[str]:
    return dir(_biosim)
