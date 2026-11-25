# SPDX-FileCopyrightText: 2025-present Demi <bjaiye1@gmail.com>
#
# SPDX-License-Identifier: MIT
from __future__ import annotations

import importlib
import importlib.util
from pkgutil import extend_path

from .__about__ import __version__
from .execution_outcome import ExecutionOutcome
from .modules import (
    BioModule,
    ExecutionContext,
    ExecutionPolicy,
    SignalEmitterBioModule,
    StatefulBioModule,
)
from .signals import (
    AcceptedSignalProfile,
    ArraySignal,
    BioSignal,
    EventSignal,
    InputValueType,
    RecordSignal,
    ScalarSignal,
    SignalEnvelope,
    SignalSpec,
    coerce_float,
    make_signal,
    scalar_or_record_input,
    unwrap_payload,
    validate_connection_specs,
    validate_port_spec_direction,
)
from .compatibility import (
    CompatibilityIssue,
    CompatibilityResult,
    NO_SAMPLE,
    check_compatibility,
    check_payload,
    compatibility_provenance,
    contract_digest,
)
from .visuals import VisualSpec, validate_visual_spec, normalize_visuals
from .visual_contract import (VISUAL_CONTRACT_VERSION, SUPPORTED_RENDERERS, visualization_catalog, audit_visualizations, validate_requirements, visual_spec_schema)
__all__ = [
    "__version__",
    "VisualSpec",
    "VISUAL_CONTRACT_VERSION",
    "SUPPORTED_RENDERERS",
    "visualization_catalog",
    "visual_spec_schema",
    "audit_visualizations",
    "validate_requirements",
    "validate_visual_spec",
    "normalize_visuals",
    "BioModule",
    "ExecutionContext",
    "ExecutionOutcome",
    "ExecutionPolicy",
    "SignalEmitterBioModule",
    "StatefulBioModule",
    "BioSignal",
    "AcceptedSignalProfile",
    "ScalarSignal",
    "ArraySignal",
    "RecordSignal",
    "EventSignal",
    "InputValueType",
    "SignalSpec",
    "SignalEnvelope",
    "unwrap_payload",
    "coerce_float",
    "scalar_or_record_input",
    "make_signal",
    "validate_connection_specs",
    "validate_port_spec_direction",
    "CompatibilityIssue",
    "CompatibilityResult",
    "NO_SAMPLE",
    "check_compatibility",
    "check_payload",
    "compatibility_provenance",
    "contract_digest",
]


_RUNTIME_EXPORTS = {
    "BioWorld": "world",
    "WorldEvent": "world",
    "WiringBuilder": "wiring",
    "build_from_spec": "wiring",
    "load_wiring": "wiring",
    "load_wiring_toml": "wiring",
    "load_wiring_yaml": "wiring",
    "build_package": "pack",
    "export_lab_package": "pack",
    "fetch_package": "pack",
    "publish_package": "pack",
    "run_package": "pack",
    "unpack_package": "pack",
    "validate_package": "pack",
    "ApiError": "cloud",
    "Artifact": "cloud",
    "AsyncClient": "cloud",
    "AuthenticationError": "cloud",
    "Client": "cloud",
    "InsufficientCreditsError": "cloud",
    "RateLimitError": "cloud",
    "Run": "cloud",
    "RunFailed": "cloud",
    "RunResult": "cloud",
    "RunTimeout": "cloud",
    "ValidationError": "cloud",
    "verify_webhook_signature": "cloud",
    "OnnxClassifierModule": "onnx",
}

# A full editable checkout and an installed SDK can live at different paths.

__path__ = extend_path(__path__, __name__)
_HAS_RUNTIME = importlib.util.find_spec("biosim.world") is not None
if _HAS_RUNTIME:
    __all__ += list(_RUNTIME_EXPORTS)


def __getattr__(name: str):
    if _HAS_RUNTIME and name == "onnx":
        return importlib.import_module(".onnx", __name__)
    if _HAS_RUNTIME and name in _RUNTIME_EXPORTS:
        return getattr(importlib.import_module("." + _RUNTIME_EXPORTS[name], __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__ + (["onnx"] if _HAS_RUNTIME else []))
