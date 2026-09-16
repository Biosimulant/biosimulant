# SPDX-FileCopyrightText: 2025-present Demi <bjaiye1@gmail.com>
#
# SPDX-License-Identifier: MIT
from __future__ import annotations

import importlib
from types import ModuleType

from .__about__ import __version__
from .world import BioWorld, WorldEvent
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
    contract_digest,
    register_checker,
    registered_types,
)
from .visuals import VisualSpec, validate_visual_spec, normalize_visuals
from .wiring import (
    WiringBuilder,
    build_from_spec,
    load_wiring,
    load_wiring_toml,
    load_wiring_yaml,
)
from .pack import (
    build_package,
    export_lab_package,
    fetch_package,
    publish_package,
    run_package,
    unpack_package,
    validate_package,
)
from .cloud import (
    ApiError,
    Artifact,
    AsyncClient,
    AuthenticationError,
    Client,
    InsufficientCreditsError,
    RateLimitError,
    Run,
    RunFailed,
    RunResult,
    RunTimeout,
    ValidationError,
    verify_webhook_signature,
)

__all__ = [
    "__version__",
    "BioWorld",
    "WorldEvent",
    "VisualSpec",
    "validate_visual_spec",
    "normalize_visuals",
    "BioModule",
    "ExecutionContext",
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
    "contract_digest",
    "register_checker",
    "registered_types",
    "WiringBuilder",
    "build_from_spec",
    "load_wiring",
    "load_wiring_toml",
    "load_wiring_yaml",
    "build_package",
    "export_lab_package",
    "fetch_package",
    "publish_package",
    "run_package",
    "unpack_package",
    "validate_package",
    "OnnxClassifierModule",
    "Client",
    "AsyncClient",
    "Run",
    "RunResult",
    "Artifact",
    "ApiError",
    "AuthenticationError",
    "ValidationError",
    "RateLimitError",
    "InsufficientCreditsError",
    "RunFailed",
    "RunTimeout",
    "verify_webhook_signature",
]


def __getattr__(name: str) -> ModuleType:
    # Lazily import optional namespaces so `import biosim` does not require extras.
    if name == "onnx":
        return importlib.import_module(".onnx", __name__)
    if name == "OnnxClassifierModule":
        return getattr(importlib.import_module(".onnx", __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted([*__all__, "onnx"])
