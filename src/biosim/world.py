from __future__ import annotations

import copy
from dataclasses import dataclass
from enum import Enum
import inspect
import logging
import threading
import warnings
from typing import Any, Callable, Dict, List, Mapping, Optional

from .execution import (
    execution_phase_findings,
    has_manifest_execution_policy,
    resolve_execution_policy,
)
from .modules import BioModule, ExecutionContext, ExecutionPolicy
from .signals import (
    BioSignal,
    SignalEnvelope,
    SignalSpec,
    make_signal,
    validate_connection_specs,
    validate_port_spec_direction,
)
from .visuals import derive_timeseries_visuals, normalize_visuals

logger = logging.getLogger(__name__)


class WorldEvent(Enum):
    """Runtime events emitted by the BioWorld orchestrator."""

    STARTED = "started"
    STEP = "step"
    FINISHED = "finished"
    ERROR = "error"
    PAUSED = "paused"
    RESUMED = "resumed"
    STOPPED = "stopped"


Listener = Callable[[WorldEvent, Dict[str, Any]], None]


class SimulationStop(Exception):
    """Internal cooperative stop signal for the run loop."""


class _ModuleExecutionContract(Enum):
    CANONICAL_EXECUTE = "canonical_execute"
    LEGACY_TEMPORAL = "legacy_temporal"


_DispatchContext = ExecutionContext | tuple[float, float]


@dataclass
class ModuleEntry:
    name: str
    module: BioModule
    input_specs: dict[str, SignalSpec]
    output_specs: dict[str, SignalSpec]
    execution_policy: ExecutionPolicy
    execution_contract: _ModuleExecutionContract


@dataclass
class Connection:
    source_module: str
    source_signal: str
    target_module: str
    target_signal: str
    last_event_time: Optional[float] = None
    last_stale_warning_time: Optional[float] = None


class BioWorld:
    """Communication-step orchestration kernel for runnable biomodules."""

    def __init__(self, *, communication_step: float) -> None:
        if communication_step <= 0:
            raise ValueError("communication_step must be positive")
        self.communication_step = float(communication_step)
        self._modules: Dict[str, ModuleEntry] = {}
        self._connections_by_target: Dict[str, List[Connection]] = {}
        self._signal_store: Dict[str, Dict[str, BioSignal]] = {}
        self._last_published_refs: set[tuple[str, str]] = set()
        self._current_time: float = 0.0
        self._is_setup: bool = False
        self._listeners: List[Listener] = []
        self._active_run_start: Optional[float] = None
        self._active_run_end: Optional[float] = None
        self._setup_config: Dict[str, Dict[str, Any]] = {}
        self._completed_once: set[str] = set()

        self._stop_requested: bool = False
        self._run_event = threading.Event()
        self._run_event.set()

    # --- Listener management -----------------------------------------
    def on(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def off(self, listener: Listener) -> None:
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass

    def _emit(self, event: WorldEvent, payload: Optional[Dict[str, Any]] = None) -> None:
        data = payload or {}
        for listener in list(self._listeners):
            try:
                listener(event, data)
            except Exception:
                logger.exception("world listener raised during %s", event)

    def _progress_payload(self, now: Optional[float] = None) -> Dict[str, float]:
        start = self._active_run_start
        end = self._active_run_end
        if start is None or end is None:
            return {}
        sim_time = self._current_time if now is None else now
        duration = max(0.0, end - start)
        if duration <= 0.0:
            progress = 1.0 if sim_time >= end else 0.0
        else:
            progress = (sim_time - start) / duration
        progress = max(0.0, min(1.0, progress))
        return {
            "start": start,
            "end": end,
            "duration": duration,
            "progress": progress,
            "progress_pct": progress * 100.0,
            "remaining": max(0.0, end - sim_time),
        }

    # --- Module registration -----------------------------------------
    def add_biomodule(
        self,
        name: str,
        module: BioModule,
    ) -> None:
        if name in self._modules and self._modules[name].module is not module:
            raise ValueError(f"Module name already registered: {name}")

        manifest_inputs = getattr(module, "_biosimulant_manifest_input_specs", None)
        manifest_outputs = getattr(module, "_biosimulant_manifest_output_specs", None)
        input_specs = self._normalize_port_specs(
            module.inputs() if manifest_inputs is None else manifest_inputs,
            direction="input",
            module_name=name,
        )
        output_specs = self._normalize_port_specs(
            module.outputs() if manifest_outputs is None else manifest_outputs,
            direction="output",
            module_name=name,
        )
        execution_policy, execution_contract = self._validate_module_execution_contract(name, module)

        try:
            setattr(module, "_world_name", name)
        except Exception:  # pragma: no cover - defensive: setattr may fail on frozen modules
            pass

        self._modules[name] = ModuleEntry(
            name=name,
            module=module,
            input_specs=input_specs,
            output_specs=output_specs,
            execution_policy=execution_policy,
            execution_contract=execution_contract,
        )

    def _validate_module_execution_contract(
        self,
        name: str,
        module: BioModule,
    ) -> tuple[ExecutionPolicy, _ModuleExecutionContract]:
        try:
            policy = resolve_execution_policy(module)
        except ValueError as exc:
            allowed = ", ".join(item.value for item in ExecutionPolicy)
            raise ValueError(
                f"Module '{name}' execution_policy must be one of: {allowed}"
            ) from exc

        uses_canonical_execute = module._uses_canonical_execute()
        overrides_execute = module._overrides_execute()
        if uses_canonical_execute and not overrides_execute:
            raise TypeError(f"Module '{name}' must implement execute() or advance_window()")
        if not uses_canonical_execute and overrides_execute:
            raise TypeError(
                f"Module '{name}' overrides both execute() and advance_window(); choose one computation hook"
            )
        if policy is not ExecutionPolicy.EACH_WINDOW and not uses_canonical_execute:
            raise TypeError(
                f"Module '{name}' uses execution_policy='{policy.value}' and must implement execute() "
                "instead of advance_window()"
            )
        if uses_canonical_execute:
            self._validate_execute_signature(name, module)
            if not self._has_explicit_execution_policy(module):
                warnings.warn(
                    f"Module '{name}' inherits execution_policy='each_window'; declare the policy "
                    "explicitly to confirm repeated invocation is intended",
                    RuntimeWarning,
                    stacklevel=3,
                )
            contract = _ModuleExecutionContract.CANONICAL_EXECUTE
        else:
            contract = _ModuleExecutionContract.LEGACY_TEMPORAL
        return policy, contract

    @staticmethod
    def _has_explicit_execution_policy(module: BioModule) -> bool:
        if has_manifest_execution_policy(module):
            return True
        instance_dict = getattr(module, "__dict__", {})
        if "execution_policy" in instance_dict:
            return True
        for cls in type(module).__mro__:
            if cls is BioModule:
                return False
            if "execution_policy" in cls.__dict__:
                return True
        return False

    @staticmethod
    def _validate_execute_signature(name: str, module: BioModule) -> None:
        expected = "execute(self, inputs, *, context)"
        try:
            signature = inspect.signature(module.execute)
        except (TypeError, ValueError) as exc:
            raise TypeError(
                f"Module '{name}' execute() signature could not be inspected; expected {expected}"
            ) from exc

        parameters = list(signature.parameters.values())
        inputs = parameters[0] if parameters else None
        context = signature.parameters.get("context")
        if (
            inputs is None
            or inputs.name != "inputs"
            or inputs.kind not in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            or inputs.default is not inspect.Parameter.empty
            or context is None
            or context.kind is not inspect.Parameter.KEYWORD_ONLY
            or context.default is not inspect.Parameter.empty
        ):
            raise TypeError(f"Module '{name}' must implement {expected}")

        for parameter in parameters:
            if parameter in (inputs, context):
                continue
            if parameter.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            if parameter.default is inspect.Parameter.empty:
                raise TypeError(f"Module '{name}' must implement {expected}")

    def _normalize_port_specs(
        self,
        specs: Mapping[str, SignalSpec] | None,
        *,
        direction: str,
        module_name: str,
    ) -> dict[str, SignalSpec]:
        if specs is None:
            return {}
        if not isinstance(specs, Mapping):
            raise TypeError(f"Module '{module_name}' {direction}s() must return a mapping of port -> SignalSpec")
        normalized: dict[str, SignalSpec] = {}
        for port, spec in specs.items():
            if not isinstance(port, str) or not port:
                raise TypeError(f"Module '{module_name}' {direction} port names must be non-empty strings")
            if isinstance(spec, Mapping):
                spec = SignalSpec.from_dict(spec)
            if not isinstance(spec, SignalSpec):
                raise TypeError(
                    f"Module '{module_name}' {direction} port '{port}' must declare a SignalSpec, got {type(spec)!r}"
                )
            validate_port_spec_direction(spec, direction=direction)
            normalized[port] = spec
        return normalized

    # --- Wiring -------------------------------------------------------
    def connect(self, source: str, target: str) -> None:
        src_parts = source.rsplit(".", 1)
        dst_parts = target.rsplit(".", 1)
        if len(src_parts) != 2 or len(dst_parts) != 2:
            raise ValueError("Source and target must be in format 'module.signal'")
        src_mod, src_sig = src_parts
        dst_mod, dst_sig = dst_parts
        if src_mod not in self._modules:
            raise KeyError(f"Unknown source module '{src_mod}'")
        if dst_mod not in self._modules:
            raise KeyError(f"Unknown target module '{dst_mod}'")

        src_entry = self._modules[src_mod]
        dst_entry = self._modules[dst_mod]
        if src_sig not in src_entry.output_specs:
            raise KeyError(f"Unknown source signal '{src_mod}.{src_sig}'")
        if dst_sig not in dst_entry.input_specs:
            raise KeyError(f"Unknown target signal '{dst_mod}.{dst_sig}'")
        validate_connection_specs(src_entry.output_specs[src_sig], dst_entry.input_specs[dst_sig])

        conn = Connection(
            source_module=src_mod,
            source_signal=src_sig,
            target_module=dst_mod,
            target_signal=dst_sig,
        )
        self._connections_by_target.setdefault(dst_mod, []).append(conn)

    # --- Setup --------------------------------------------------------
    def setup(self, config: Optional[Dict[str, Any]] = None) -> None:
        config = config or {}
        self._setup_config = {name: dict(module_cfg or {}) for name, module_cfg in config.items()}
        self._signal_store = {}
        self._last_published_refs = set()
        self._completed_once = set()
        self._current_time = 0.0
        for connections in self._connections_by_target.values():
            for conn in connections:
                conn.last_event_time = None
                conn.last_stale_warning_time = None

        for entry in self._modules.values():
            entry.module._reset_execution_adapter()
            entry.module.setup(self._setup_config.get(entry.name, {}))
            outputs = self._normalize_outputs(entry.name, entry.module.get_outputs() or {})
            self._commit_outputs(entry.name, outputs)

        self._is_setup = True

    def _normalize_outputs(self, module_name: str, outputs: Mapping[str, BioSignal]) -> Dict[str, BioSignal]:
        if not isinstance(outputs, Mapping):
            raise TypeError(f"Module '{module_name}' get_outputs() must return a mapping")
        declared = self._modules[module_name].output_specs
        normalized: Dict[str, BioSignal] = {}
        for port, signal in outputs.items():
            if port not in declared:
                raise KeyError(f"Module '{module_name}' produced undeclared output port '{port}'")
            if not isinstance(signal, BioSignal):
                raise TypeError(
                    f"Module '{module_name}' output '{port}' must be a typed BioSignal, got {type(signal)!r}"
                )
            compatibility_envelope = getattr(signal, "compatibility_envelope", None)
            if isinstance(compatibility_envelope, SignalEnvelope):
                compatibility_envelope.validate_contract(declared[port].contract)
            bound = signal.with_spec(declared[port]) if signal.spec is None else signal.with_spec(declared[port])
            if bound.source != module_name:
                previous = bound
                bound = previous.__class__(
                    source=module_name,
                    name=previous.name,
                    value=copy.deepcopy(previous.value),
                    emitted_at=previous.emitted_at,
                    spec=previous.spec,
                )
                previous._copy_compatibility_envelope(bound)
            if bound.name != port:
                bound = bound.retarget(name=port)
            normalized[port] = bound
        return normalized

    def _normalize_canonical_outputs(
        self,
        module_name: str,
        outputs: Mapping[str, Any | BioSignal],
        *,
        emitted_at: float,
    ) -> Dict[str, BioSignal]:
        if not isinstance(outputs, Mapping):
            raise TypeError(f"Module '{module_name}' execute() must return a mapping")
        declared = self._modules[module_name].output_specs
        normalized: Dict[str, BioSignal] = {}
        for port, value in outputs.items():
            if not isinstance(port, str) or not port:
                raise TypeError(
                    f"Module '{module_name}' execute() output port names must be non-empty strings"
                )
            if port not in declared:
                raise KeyError(f"Module '{module_name}' produced undeclared output port '{port}'")
            compatibility_envelope = None
            if (
                isinstance(value, Mapping)
                and value.get("schema_version") == "0.1"
                and "contract_digest" in value
            ):
                compatibility_envelope = SignalEnvelope.from_dict(value)
                compatibility_envelope.validate_contract(declared[port].contract)
                payload = compatibility_envelope.payload
            else:
                payload = value.value if isinstance(value, BioSignal) else value
            signal = make_signal(
                declared[port],
                source=module_name,
                name=port,
                value=payload,
                emitted_at=float(emitted_at),
            )
            if compatibility_envelope is not None:
                signal.compatibility_envelope = compatibility_envelope
            normalized[port] = signal
        return normalized

    def _commit_outputs(self, module_name: str, outputs: Mapping[str, BioSignal]) -> None:
        if not outputs:
            return
        self._signal_store[module_name] = dict(outputs)

    def _warn_if_input_stale(self, conn: Connection, source_signal: BioSignal, target_spec: SignalSpec, now: float) -> None:
        if source_signal.kind == "event":
            return
        if target_spec.max_age is None:
            return
        age = now - source_signal.emitted_at
        if age - target_spec.max_age <= 1e-12:
            return
        if conn.last_stale_warning_time is not None and source_signal.emitted_at <= conn.last_stale_warning_time:
            return

        if target_spec.stale_policy == "ignore":
            return
        if target_spec.stale_policy == "error":
            raise ValueError(
                f"stale signal read: target '{conn.target_module}' consumed '{conn.source_module}.{conn.source_signal}' "
                f"at t={now:.6f} using source time {source_signal.emitted_at:.6f} "
                f"(age={age:.6f} > max_age={target_spec.max_age:.6f})"
            )

        conn.last_stale_warning_time = source_signal.emitted_at
        logger.warning(
            "stale signal read: target '%s' consumed '%s.%s' at t=%.6f using source time %.6f "
            "(age=%.6f > max_age=%.6f)",
            conn.target_module,
            conn.source_module,
            conn.source_signal,
            now,
            source_signal.emitted_at,
            age,
            target_spec.max_age,
        )

    def _collect_inputs(self, target_name: str, start: float) -> Dict[str, BioSignal]:
        inputs: Dict[str, BioSignal] = {}
        entry = self._modules[target_name]
        for conn in self._connections_by_target.get(target_name, []):
            source_outputs = self._signal_store.get(conn.source_module, {})
            source_signal = source_outputs.get(conn.source_signal)
            if source_signal is None:
                continue
            target_spec = entry.input_specs[conn.target_signal]
            source_spec = self._modules[conn.source_module].output_specs[conn.source_signal]
            validate_connection_specs(
                source_spec,
                target_spec,
                sample=source_signal.value,
                check_sample=True,
                suppressed_warning_codes=("PROFILE_PARTIAL",),
            )
            self._warn_if_input_stale(conn, source_signal, target_spec, start)
            if source_signal.kind == "event":
                if conn.last_event_time is not None and source_signal.emitted_at <= conn.last_event_time:
                    continue
                conn.last_event_time = source_signal.emitted_at
            inputs[conn.target_signal] = source_signal.retarget(name=conn.target_signal)
        return inputs

    def _execute_inputs_ready(
        self,
        target_name: str,
        *,
        once_policy: ExecutionPolicy | None = None,
    ) -> bool:
        """Return whether every non-optional connected input is current and available."""

        entry = self._modules[target_name]
        for conn in self._connections_by_target.get(target_name, []):
            target_spec = entry.input_specs[conn.target_signal]
            if target_spec.required is False:
                continue
            source_entry = self._modules[conn.source_module]
            if (
                once_policy is not None
                and source_entry.execution_policy is once_policy
                and conn.source_module not in self._completed_once
            ):
                return False
            source_outputs = self._signal_store.get(conn.source_module, {})
            if conn.source_signal not in source_outputs:
                return False
        return True

    def _missing_execute_inputs(
        self,
        target_name: str,
        *,
        once_policy: ExecutionPolicy | None = None,
    ) -> list[str]:
        entry = self._modules[target_name]
        missing: list[str] = []
        for conn in self._connections_by_target.get(target_name, []):
            target_spec = entry.input_specs[conn.target_signal]
            if target_spec.required is False:
                continue
            source_entry = self._modules[conn.source_module]
            if (
                once_policy is not None
                and source_entry.execution_policy is once_policy
                and conn.source_module not in self._completed_once
            ):
                missing.append(
                    f"{conn.source_module}.{conn.source_signal}->{target_name}.{conn.target_signal} "
                    "(upstream not completed in this run)"
                )
                continue
            source_outputs = self._signal_store.get(conn.source_module, {})
            if conn.source_signal not in source_outputs:
                missing.append(f"{conn.source_module}.{conn.source_signal}->{target_name}.{conn.target_signal}")
        return missing

    def _execute_module(
        self,
        name: str,
        entry: ModuleEntry,
        inputs: Dict[str, BioSignal],
        context: _DispatchContext,
    ) -> Dict[str, BioSignal]:
        if entry.execution_contract is _ModuleExecutionContract.CANONICAL_EXECUTE:
            if not isinstance(context, ExecutionContext):  # pragma: no cover - internal invariant
                raise RuntimeError("canonical execution requires an ExecutionContext")
            canonical_inputs = dict(getattr(entry.module, "_execution_inputs", {}))
            canonical_inputs.update(inputs)
            outputs = entry.module.execute(canonical_inputs, context=context)
            return self._normalize_canonical_outputs(
                name,
                outputs,
                emitted_at=context.simulated_time,
            )

        if inputs:
            entry.module.set_inputs(inputs)
        if isinstance(context, ExecutionContext):
            if context.window_start is None or context.window_end is None:
                raise RuntimeError(
                    f"Module '{name}' uses the temporal compatibility contract outside a communication window"
                )
            window_start, window_end = context.window_start, context.window_end
        else:
            window_start, window_end = context
        entry.module.advance_window(window_start, window_end)
        return self._normalize_outputs(name, entry.module.get_outputs() or {})

    def _commit_invocation_outputs(
        self,
        pending_outputs: Mapping[str, Mapping[str, BioSignal]],
    ) -> None:
        for name, outputs in pending_outputs.items():
            self._commit_outputs(name, outputs)
            entry = self._modules[name]
            if entry.execution_contract is _ModuleExecutionContract.CANONICAL_EXECUTE:
                entry.module._restore_execution_outputs(outputs)

    def _validate_execution_graph(self) -> None:
        findings = execution_phase_findings(
            self.execution_policies,
            (
                (conn.source_module, target)
                for target, connections in self._connections_by_target.items()
                for conn in connections
            ),
        )
        if findings:
            raise ValueError(findings[0])

    @property
    def execution_policies(self) -> Dict[str, ExecutionPolicy]:
        """Resolved invocation policy for each registered module, in registration order."""

        return {name: entry.execution_policy for name, entry in self._modules.items()}

    def _drain_once_phase(self, policy: ExecutionPolicy, timestamp: float) -> None:
        if self._active_run_start is None or self._active_run_end is None:
            raise RuntimeError("once-policy execution requires an active positive-duration run")
        remaining = [
            name
            for name, entry in self._modules.items()
            if entry.execution_policy is policy and name not in self._completed_once
        ]
        while remaining:
            if self._stop_requested:
                raise SimulationStop()
            ready = [
                name
                for name in remaining
                if self._execute_inputs_ready(name, once_policy=policy)
            ]
            if not ready:
                details = []
                for name in remaining:
                    missing = self._missing_execute_inputs(name, once_policy=policy)
                    details.append(f"{name}: {', '.join(missing) if missing else 'unresolved dependency'}")
                raise RuntimeError(
                    f"{policy.value} phase could not resolve required inputs ({'; '.join(details)})"
                )

            context = ExecutionContext(
                policy=policy,
                run_start=self._active_run_start,
                run_end=self._active_run_end,
            )
            pending_outputs: Dict[str, Dict[str, BioSignal]] = {}
            for name in ready:
                if self._stop_requested:
                    raise SimulationStop()
                entry = self._modules[name]
                inputs = self._collect_inputs(name, timestamp)
                pending_outputs[name] = self._execute_module(name, entry, inputs, context)
                if self._stop_requested:
                    raise SimulationStop()

            self._commit_invocation_outputs(pending_outputs)
            self._completed_once.update(ready)
            self._last_published_refs = {
                (name, port)
                for name, outputs in pending_outputs.items()
                for port in outputs.keys()
            }
            remaining = [name for name in remaining if name not in self._completed_once]

    # --- Run loop -----------------------------------------------------
    def run(self, duration: float) -> None:
        if duration <= 0:
            if not self._is_setup:
                self.setup()
            return

        self._validate_execution_graph()
        if not self._is_setup:
            self.setup()

        eps = 1e-12
        end_time = self._current_time + duration
        self._active_run_start = self._current_time
        self._active_run_end = end_time
        self._completed_once = set()
        for name, entry in self._modules.items():
            if entry.execution_policy is ExecutionPolicy.EACH_WINDOW:
                continue
            entry.module._clear_execution_outputs()
            self._signal_store.pop(name, None)

        self._stop_requested = False
        self._run_event.set()
        self._emit(WorldEvent.STARTED, {"t": self._current_time, **self._progress_payload(self._current_time)})

        try:
            self._drain_once_phase(ExecutionPolicy.ONCE_BEFORE_RUN, self._current_time)

            has_each_window = any(
                entry.execution_policy is ExecutionPolicy.EACH_WINDOW
                for entry in self._modules.values()
            )
            has_canonical_each_window = any(
                entry.execution_policy is ExecutionPolicy.EACH_WINDOW
                and entry.execution_contract is _ModuleExecutionContract.CANONICAL_EXECUTE
                for entry in self._modules.values()
            )

            if not has_each_window and self._current_time < end_time - eps:
                # No module consumes communication windows, so stepping through
                # them would only emit empty turns. Cross the run in one step.
                if self._stop_requested:
                    raise SimulationStop()
                self._run_event.wait()
                if self._stop_requested:
                    raise SimulationStop()
                window_start = self._current_time
                self._last_published_refs = set()
                self._current_time = end_time
                self._emit(
                    WorldEvent.STEP,
                    {
                        "t": self._current_time,
                        "window_start": window_start,
                        "window_end": end_time,
                        **self._progress_payload(self._current_time),
                    },
                )

            while self._current_time < end_time - eps:
                if self._stop_requested:
                    raise SimulationStop()

                self._run_event.wait()

                if self._stop_requested:
                    raise SimulationStop()

                window_start = self._current_time
                window_end = min(window_start + self.communication_step, end_time)
                context: _DispatchContext
                if has_canonical_each_window:
                    context = ExecutionContext(
                        policy=ExecutionPolicy.EACH_WINDOW,
                        run_start=self._active_run_start,
                        run_end=self._active_run_end,
                        window_start=window_start,
                        window_end=window_end,
                    )
                else:
                    # Existing all-temporal worlds avoid paying to construct a
                    # public context no model can observe.
                    context = (window_start, window_end)
                window_inputs: Dict[str, Dict[str, BioSignal]] = {}
                for name, entry in self._modules.items():
                    if entry.execution_policy is not ExecutionPolicy.EACH_WINDOW:
                        continue
                    if (
                        entry.execution_contract is _ModuleExecutionContract.CANONICAL_EXECUTE
                        and not self._execute_inputs_ready(name)
                    ):
                        continue
                    window_inputs[name] = self._collect_inputs(name, window_start)

                pending_outputs: Dict[str, Dict[str, BioSignal]] = {}
                for name, entry in self._modules.items():
                    if name not in window_inputs:
                        continue
                    pending_outputs[name] = self._execute_module(
                        name,
                        entry,
                        window_inputs[name],
                        context,
                    )
                    if self._stop_requested:
                        raise SimulationStop()

                self._commit_invocation_outputs(pending_outputs)

                self._last_published_refs = {
                    (name, port)
                    for name, outputs in pending_outputs.items()
                    for port in outputs.keys()
                }
                self._current_time = window_end

                self._emit(
                    WorldEvent.STEP,
                    {
                        "t": self._current_time,
                        "window_start": window_start,
                        "window_end": window_end,
                        **self._progress_payload(self._current_time),
                    },
                )

            self._drain_once_phase(ExecutionPolicy.ONCE_AFTER_RUN, self._current_time)

        except SimulationStop:
            self._emit(WorldEvent.STOPPED, {"t": self._current_time, **self._progress_payload(self._current_time)})
        except Exception as exc:
            self._emit(
                WorldEvent.ERROR,
                {"t": self._current_time, "error": exc, **self._progress_payload(self._current_time)},
            )
            raise
        finally:
            self._emit(WorldEvent.FINISHED, {"t": self._current_time, **self._progress_payload(self._current_time)})
            self._active_run_start = None
            self._active_run_end = None

    def settle(self, steps: int = 1) -> None:
        """Propagate final committed outputs through downstream modules.

        Settling performs zero-time communication turns after a run. It is
        opt-in and does not advance simulated time.
        """
        if isinstance(steps, bool) or not isinstance(steps, int):
            raise TypeError("settle steps must be an integer")
        if steps < 0:
            raise ValueError("settle steps must be non-negative")
        if steps == 0:
            return
        if not self._is_setup:
            self.setup()

        frontier = set(self._last_published_refs)
        for _ in range(steps):
            if self._stop_requested:
                break
            if not frontier:
                break

            target_names = {
                conn.target_module
                for conns in self._connections_by_target.values()
                for conn in conns
                if (conn.source_module, conn.source_signal) in frontier
            }
            if not target_names:
                break

            pending_outputs: Dict[str, Dict[str, BioSignal]] = {}
            window_time = self._current_time
            for name, entry in self._modules.items():
                if self._stop_requested:
                    break
                if name not in target_names:
                    continue
                if entry.execution_contract is _ModuleExecutionContract.CANONICAL_EXECUTE:
                    continue
                inputs = self._collect_inputs(name, window_time)
                if inputs:
                    entry.module.set_inputs(inputs)
                entry.module.advance_window(window_time, window_time)
                pending_outputs[name] = self._normalize_outputs(name, entry.module.get_outputs() or {})

            for name, outputs in pending_outputs.items():
                self._commit_outputs(name, outputs)

            frontier = {
                (name, port)
                for name, outputs in pending_outputs.items()
                for port in outputs.keys()
            }
            self._last_published_refs = set(frontier)

    # --- Cooperative controls ----------------------------------------
    def request_stop(self) -> None:
        self._stop_requested = True
        self._run_event.set()
        for entry in list(self._modules.values()):
            try:
                entry.module.request_stop()
            except Exception:
                logger.exception("BioModule.request_stop raised for %s", entry.name)

    def request_pause(self) -> None:
        self._run_event.clear()
        self._emit(WorldEvent.PAUSED, {"t": self._current_time, **self._progress_payload(self._current_time)})

    def request_resume(self) -> None:
        self._run_event.set()
        self._emit(WorldEvent.RESUMED, {"t": self._current_time, **self._progress_payload(self._current_time)})

    # --- Snapshot / restore ------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        return {
            "communication_step": self.communication_step,
            "current_time": self._current_time,
            "is_setup": self._is_setup,
            "setup_config": copy.deepcopy(self._setup_config),
            "signal_store": {
                module_name: {port: signal.to_dict() for port, signal in outputs.items()}
                for module_name, outputs in self._signal_store.items()
            },
            "last_published_refs": [
                {"module": module_name, "port": port}
                for module_name, port in sorted(self._last_published_refs)
            ],
            "completed_once": sorted(self._completed_once),
            "connections": {
                target: [
                    {
                        "source_module": conn.source_module,
                        "source_signal": conn.source_signal,
                        "target_module": conn.target_module,
                        "target_signal": conn.target_signal,
                        "last_event_time": conn.last_event_time,
                        "last_stale_warning_time": conn.last_stale_warning_time,
                    }
                    for conn in conns
                ]
                for target, conns in self._connections_by_target.items()
            },
            "modules": {
                name: copy.deepcopy(entry.module.snapshot())
                for name, entry in self._modules.items()
            },
        }

    def restore(self, snapshot: Mapping[str, Any]) -> None:
        if "time_unit" in snapshot or "signal_history" in snapshot:
            raise ValueError("snapshot uses removed world fields")
        if not self._is_setup:
            setup_config = snapshot.get("setup_config")
            self.setup(dict(copy.deepcopy(setup_config)) if isinstance(setup_config, Mapping) else None)

        module_states = snapshot.get("modules")
        if not isinstance(module_states, Mapping):
            raise ValueError("snapshot is missing module state")
        for name, entry in self._modules.items():
            if name not in module_states:
                raise KeyError(f"snapshot missing module state for '{name}'")
            entry.module.restore(copy.deepcopy(module_states[name]))

        self._current_time = float(snapshot.get("current_time", 0.0))
        self._is_setup = bool(snapshot.get("is_setup", True))
        self._setup_config = copy.deepcopy(snapshot.get("setup_config", {}))

        signal_store: Dict[str, Dict[str, BioSignal]] = {}
        for module_name, outputs in snapshot.get("signal_store", {}).items():
            signal_store[module_name] = {
                port: BioSignal.from_dict(signal_dict)
                for port, signal_dict in outputs.items()
            }
        self._signal_store = signal_store
        self._completed_once = {
            str(name)
            for name in snapshot.get("completed_once", [])
            if str(name) in self._modules
        }
        for name, entry in self._modules.items():
            if entry.execution_contract is _ModuleExecutionContract.CANONICAL_EXECUTE:
                entry.module._restore_execution_outputs(self._signal_store.get(name, {}))
        self._last_published_refs = {
            (str(ref["module"]), str(ref["port"]))
            for ref in snapshot.get("last_published_refs", [])
            if isinstance(ref, Mapping) and ref.get("module") and ref.get("port")
        }

        snapshot_connections = snapshot.get("connections", {})
        if not isinstance(snapshot_connections, Mapping):
            raise ValueError("snapshot connections must be a mapping")
        for target, conns in self._connections_by_target.items():
            raw_conns = snapshot_connections.get(target, [])
            if len(raw_conns) != len(conns):
                raise ValueError(f"snapshot connection count mismatch for target '{target}'")
            for conn, raw in zip(conns, raw_conns):
                conn.last_event_time = raw.get("last_event_time")
                conn.last_stale_warning_time = raw.get("last_stale_warning_time")

    def branch(self) -> "BioWorld":
        snapshot = self.snapshot()
        branched = BioWorld(communication_step=self.communication_step)
        for name, entry in self._modules.items():
            try:
                module_copy = copy.deepcopy(entry.module)
            except Exception as exc:  # pragma: no cover - depends on module implementation
                raise TypeError(
                    f"Module '{name}' could not be deep-copied for branching; implement deepcopy-safe state"
                ) from exc
            branched.add_biomodule(name, module_copy)
        for target, connections in self._connections_by_target.items():
            for conn in connections:
                branched.connect(f"{conn.source_module}.{conn.source_signal}", f"{target}.{conn.target_signal}")
        branched.restore(copy.deepcopy(snapshot))
        return branched

    # --- Introspection ------------------------------------------------
    @property
    def current_time(self) -> float:
        return self._current_time

    @property
    def module_names(self) -> List[str]:
        return list(self._modules.keys())

    def get_outputs(self, name: str) -> Dict[str, BioSignal]:
        return self._signal_store.get(name, {})

    def collect_visuals(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for entry in self._modules.values():
            module = entry.module
            try:
                visuals = module.visualize()  # type: ignore[attr-defined]
            except Exception:
                logger.exception("BioModule.visualize raised for %s", module.__class__.__name__)
                continue
            if not visuals:
                visuals = derive_timeseries_visuals(
                    self._signal_store.get(entry.name, {})
                )
            normalized = normalize_visuals(visuals)
            if normalized:
                out.append({"module": entry.name, "visuals": normalized})
        return out
