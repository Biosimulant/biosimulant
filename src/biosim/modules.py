from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from enum import Enum
import math
from numbers import Real
from typing import Any, Dict, List, Mapping, Optional, TYPE_CHECKING

from .signals import BioSignal, SignalSpec, make_signal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .visuals import VisualSpec


class ExecutionPolicy(str, Enum):
    """Control when a canonical BioModule is invoked by BioWorld."""

    ONCE_BEFORE_RUN = "once_before_run"
    EACH_WINDOW = "each_window"
    ONCE_AFTER_RUN = "once_after_run"


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Immutable simulated-time context for one canonical module invocation."""

    policy: ExecutionPolicy
    run_start: float
    run_end: float
    window_start: float | None = None
    window_end: float | None = None

    def __post_init__(self) -> None:
        try:
            policy = ExecutionPolicy(self.policy)
        except (TypeError, ValueError) as exc:
            allowed = ", ".join(item.value for item in ExecutionPolicy)
            raise ValueError(f"policy must be one of: {allowed}") from exc
        object.__setattr__(self, "policy", policy)

        for field_name in ("run_start", "run_end", "window_start", "window_end"):
            value = getattr(self, field_name)
            if value is None and field_name.startswith("window_"):
                continue
            if isinstance(value, bool) or not isinstance(value, Real):
                raise TypeError(f"{field_name} must be a finite number")
            normalized = float(value)
            if not math.isfinite(normalized):
                raise ValueError(f"{field_name} must be finite")
            object.__setattr__(self, field_name, normalized)

        if self.run_end <= self.run_start:
            raise ValueError("run_end must be greater than run_start")

        if policy is ExecutionPolicy.EACH_WINDOW:
            if self.window_start is None or self.window_end is None:
                raise ValueError("each_window context requires window_start and window_end")
            if not (
                self.run_start <= self.window_start < self.window_end <= self.run_end
            ):
                raise ValueError(
                    "each_window context requires run_start <= window_start < window_end <= run_end"
                )
        elif self.window_start is not None or self.window_end is not None:
            raise ValueError("once-policy context must not include window boundaries")

    @property
    def simulated_time(self) -> float:
        """Return the BioWorld boundary that timestamps canonical outputs."""

        if self.policy is ExecutionPolicy.ONCE_BEFORE_RUN:
            return self.run_start
        if self.policy is ExecutionPolicy.EACH_WINDOW:
            assert self.window_end is not None
            return self.window_end
        return self.run_end


class BioModule(ABC):
    """Runnable model interface for the communication-step kernel.

    New modules implement :meth:`execute` and declare when BioWorld should invoke
    them. Existing temporal modules can continue overriding
    :meth:`advance_window`; that supported compatibility contract retains its
    established ``set_inputs()`` and ``get_outputs()`` lifecycle.
    """

    execution_policy: ExecutionPolicy | str = ExecutionPolicy.EACH_WINDOW

    def setup(self, config: Optional[Dict[str, Any]] = None) -> None:
        """Initialize the module for a run."""
        return

    def reset(self) -> None:
        """Reset the module to its initial state."""
        self._reset_execution_adapter()
        return

    def set_inputs(self, signals: Dict[str, BioSignal]) -> None:
        """Receive committed inputs for the current communication window."""
        self._execution_inputs = dict(signals or {})
        return

    def execute(
        self,
        inputs: Mapping[str, BioSignal],
        *,
        context: ExecutionContext,
    ) -> Mapping[str, Any | BioSignal]:
        """Run one canonical computation under an explicit invocation context."""

        raise NotImplementedError("BioModule must implement execute() or advance_window()")

    def advance_window(self, start: float, end: float) -> Dict[str, BioSignal]:
        """Advance an existing temporal module across one communication window."""

        raise RuntimeError(
            "canonical execute BioModules must be invoked through BioWorld; "
            "override advance_window() only for the supported temporal compatibility contract"
        )

    def get_outputs(self) -> Dict[str, BioSignal]:
        """Return current output signals for atomic boundary commit."""

        return dict(getattr(self, "_execution_outputs", {}))

    def _reset_execution_adapter(self) -> None:
        """Clear adapter-owned inputs and outputs without relying on super()."""

        self._execution_inputs = {}
        self._clear_execution_outputs()

    def _clear_execution_outputs(self) -> None:
        """Clear only the adapter-owned latest execution result."""

        self._execution_outputs: Dict[str, BioSignal] = {}

    def _restore_execution_outputs(self, outputs: Mapping[str, BioSignal]) -> None:
        """Restore adapter-owned output state from a BioWorld snapshot."""

        self._execution_outputs = dict(outputs)

    def _uses_canonical_execute(self) -> bool:
        """Return whether this module inherits the canonical advance guard."""

        return type(self).advance_window is BioModule.advance_window

    def _overrides_execute(self) -> bool:
        return type(self).execute is not BioModule.execute

    def inputs(self) -> Mapping[str, SignalSpec]:
        """Declared input port specifications."""
        return {}

    def outputs(self) -> Mapping[str, SignalSpec]:
        """Declared output port specifications."""
        return {}

    def snapshot(self) -> Dict[str, Any]:
        """Return serializable module state for branching/restoration."""
        return {}

    def restore(self, snapshot: Mapping[str, Any]) -> None:
        """Restore module state from a prior snapshot."""
        return

    def request_stop(self) -> None:
        """Request cooperative cancellation for blocking module work."""
        return

    def visualize(self) -> Optional["VisualSpec" | List["VisualSpec"]]:
        """Return portable VisualSpec(s) from computed state; see visualization_catalog().

        Result modules normally implement this. Plumbing modules may omit it.
        Visualization supplements, and never replaces, typed scientific outputs.
        """
        return None


class SignalEmitterBioModule(BioModule):
    """Optional BioModule base for modules that publish typed output signals.

    It centralizes the common ``_outputs`` storage and value-to-signal wrapping
    pattern for temporal compatibility modules. Canonical subclasses use the
    BioWorld-owned latest result from :class:`BioModule`.
    """

    def __init__(self) -> None:
        self._outputs: Dict[str, BioSignal] = {}

    def source_name(self) -> str:
        """Return the source name used for emitted signals."""

        return str(getattr(self, "_world_name", self.__class__.__name__))

    def emit_signal(
        self,
        name: str,
        value: Any,
        emitted_at: float,
        *,
        spec: SignalSpec | Mapping[str, Any] | None = None,
    ) -> BioSignal:
        """Build one typed output signal for a declared output port."""

        if spec is None:
            spec = self.outputs().get(name)
        return make_signal(
            spec,
            source=self.source_name(),
            name=name,
            value=value,
            emitted_at=float(emitted_at),
        )

    def output_payload(self, t: float) -> Mapping[str, Any]:
        """Return raw output values keyed by output port.

        Subclasses can override this and call ``publish_outputs()`` from their
        lifecycle code.
        """

        return {}

    def publish_outputs(self, t: float, payloads: Mapping[str, Any] | None = None) -> None:
        """Wrap raw output payloads into typed BioSignals."""

        payloads = self.output_payload(t) if payloads is None else payloads
        specs = self.outputs()
        self._outputs = {
            name: self.emit_signal(name, value, float(t), spec=specs.get(name))
            for name, value in payloads.items()
        }

    def get_outputs(self) -> Dict[str, BioSignal]:
        """Return current output signals for atomic boundary commit."""

        if self._uses_canonical_execute():
            return super().get_outputs()
        return dict(getattr(self, "_outputs", {}))

    def clear_outputs(self) -> None:
        self._outputs = {}


class StatefulBioModule(SignalEmitterBioModule):
    """Optional base for fixed-step stateful modules.

    Subclasses provide the domain behavior through hooks. Modules with unusual
    timing or solver requirements should continue inheriting directly from
    ``BioModule`` or ``SignalEmitterBioModule``.
    """

    def __init__(
        self,
        *,
        integration_step: float = 1.0,
        max_history_points: int = 10000,
        record_initial_state: bool = False,
        publish_on_setup: bool = False,
        publish_on_zero_window: bool = True,
    ) -> None:
        super().__init__()
        if integration_step <= 0:
            raise ValueError("integration_step must be positive")
        if max_history_points <= 0:
            raise ValueError("max_history_points must be positive")
        self.integration_step = float(integration_step)
        self.max_history_points = int(max_history_points)
        self.record_initial_state = bool(record_initial_state)
        self.publish_on_setup = bool(publish_on_setup)
        self.publish_on_zero_window = bool(publish_on_zero_window)
        self._time = 0.0
        self._input_overrides: Dict[str, BioSignal] = {}
        self._history: List[Any] = []

    @property
    def history(self) -> List[Any]:
        return list(getattr(self, "_history", []))

    def reset_state(self) -> None:
        """Reset subclass-owned state."""

        return

    def apply_overrides(self, *, reset_initial_state: bool) -> None:
        """Apply currently stored input overrides to subclass state."""

        return

    def step(self, h: float) -> None:
        """Advance subclass state by one internal step."""

        return

    def record_state(self, t: float) -> None:
        """Record subclass state at time ``t``."""

        return

    def setup(self, config: Optional[Dict[str, Any]] = None) -> None:
        self.reset()
        if self.publish_on_setup:
            self.publish_outputs(self._time)

    def reset(self) -> None:
        self._time = 0.0
        self._input_overrides = {}
        self._history = []
        self.clear_outputs()
        self.reset_state()

    def set_inputs(self, signals: Dict[str, BioSignal]) -> None:
        self._input_overrides = dict(signals or {})
        self.apply_overrides(reset_initial_state=self._time <= 0.0 and not self._history)

    def advance_window(
        self,
        start: float | None = None,
        end: float | None = None,
        inputs: Dict[str, BioSignal] | None = None,
    ) -> Dict[str, BioSignal]:
        if inputs is not None:
            self.set_inputs(inputs)
        else:
            self.apply_overrides(reset_initial_state=False)

        if self.record_initial_state and not self._history:
            self.record_state(self._time)
            self.trim_history()

        if end is None:
            end = self._time + float(getattr(self, "communication_step", self.integration_step) or self.integration_step)
        target = float(end)
        if target <= self._time:
            if self.publish_on_zero_window:
                self.publish_outputs(self._time)
            return self.get_outputs()

        current = self._time
        while current < target - 1e-12:
            h = min(self.integration_step, target - current)
            self.step(h)
            current += h
            self._time = current
            self.record_state(current)
            self.trim_history()

        self.publish_outputs(self._time)
        return self.get_outputs()

    def trim_history(self) -> None:
        history = getattr(self, "_history", None)
        if isinstance(history, list) and len(history) > self.max_history_points:
            del history[:-self.max_history_points]
