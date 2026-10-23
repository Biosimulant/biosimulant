"""Explicit computational outcomes, independent of the contents of user results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

_STATES = {"completed", "partial", "failed"}


@dataclass(frozen=True)
class ExecutionOutcome:
    """What a computation did; no scientific acceptance or validity judgment."""

    state: str = "completed"
    stage: str | None = None
    error: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.state not in _STATES:
            raise ValueError("execution outcome must be completed, partial or failed")
        if self.stage is not None and not isinstance(self.stage, str):
            raise TypeError("execution stage must be a string")
        if self.error is not None and (
            not isinstance(self.error, Mapping)
            or not isinstance(self.error.get("message"), str)
        ):
            raise ValueError("execution error must include a message")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"schema_version": "1", "state": self.state}
        if self.stage is not None:
            result["stage"] = self.stage
        if self.error is not None:
            result["error"] = {
                key: str(value)[:2000]
                for key, value in self.error.items()
                if key in {"code", "message", "record_id"} and value is not None
            }
        return result

    @classmethod
    def from_returncode(
        cls, returncode: int, *, stage: str | None = None,
        message: str | None = None,
    ) -> "ExecutionOutcome":
        """Capture a child program's failure even when its adapter retains results."""
        if type(returncode) is not int:
            raise TypeError("returncode must be an integer")
        return cls(
            state="completed" if returncode == 0 else "failed",
            stage=stage,
            error=None if returncode == 0 else {
                "code": "command_failed",
                "message": message or f"Command exited with code {returncode}.",
            },
        )


def normalize_outcome(value: Any) -> dict[str, Any] | None:
    """Accept only the explicit versioned protocol, never arbitrary status fields."""
    if isinstance(value, ExecutionOutcome):
        return value.to_dict()
    if not isinstance(value, Mapping) or value.get("schema_version") != "1":
        return None
    if value.get("state") not in _STATES:
        return None
    try:
        return ExecutionOutcome(value["state"], value.get("stage"), value.get("error")).to_dict()
    except (TypeError, ValueError):
        return None


def aggregate_outcomes(components: Mapping[str, Any]) -> dict[str, Any]:
    rows = []
    for name, value in components.items():
        outcome = normalize_outcome(value)
        if value is not None and outcome is None:
            raise ValueError(f"Module {name!r} reported an invalid execution outcome")
        rows.append({"module": name, **(outcome or ExecutionOutcome().to_dict())})
    state = (
        "failed" if any(r["state"] == "failed" for r in rows)
        else "partial" if any(r["state"] == "partial" for r in rows)
        else "completed"
    )
    result: dict[str, Any] = {"schema_version": "1", "state": state, "components": rows}
    problem = next((r for r in rows if r["state"] == state and state != "completed"), None)
    if problem:
        for key in ("stage", "error"):
            if key in problem:
                result[key] = problem[key]
    return result
