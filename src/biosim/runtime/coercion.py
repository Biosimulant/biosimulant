"""Typed runtime input coercion for Biosimulant modules.

This module is provisional. It converts raw ``runtime.initial_inputs`` payloads
into typed BioSignal instances by matching declared input SignalSpec profiles.
"""

from __future__ import annotations

from typing import Any, Mapping


def _merge_contract(base: Mapping[str, Any], refinement: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in refinement.items():
        current = result.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            result[key] = _merge_contract(current, value)
        else:
            result[key] = value
    return result


def _raise(
    error_cls: type[Exception], message: str, cause: BaseException | None = None
) -> None:
    error = error_cls(message)
    if cause is not None:
        raise error from cause
    raise error


def _normalize_shape_tuple(value: Any) -> tuple[Any, ...] | None:
    if isinstance(value, tuple):
        return value
    if isinstance(value, list):
        return tuple(value)
    return None


def _normalize_schema_dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return dict(value)
    return None


def _infer_signal_type(value: Any) -> str | None:
    if isinstance(value, dict):
        return "record"
    if isinstance(value, (list, tuple)) or hasattr(value, "shape"):
        return "array"
    if value is None:
        return None
    return "scalar"


def _matching_input_profiles(
    declared_spec: Any,
    *,
    signal_type: str | None,
    dtype: str | None,
    shape: tuple[Any, ...] | None,
    schema: dict[str, Any] | None,
    emitted_unit: str | None,
) -> list[Any]:
    matches: list[Any] = []
    for profile in declared_spec.input_profiles():
        if (
            profile.signal_type is not None
            and signal_type is not None
            and profile.signal_type != signal_type
        ):
            continue
        if profile.dtype is not None and dtype is not None and profile.dtype != dtype:
            continue
        if (
            profile.shape is not None
            and shape is not None
            and tuple(profile.shape) != tuple(shape)
        ):
            continue
        if (
            profile.schema is not None
            and schema is not None
            and profile.schema != schema
        ):
            continue
        if (
            profile.accepted_units is not None
            and emitted_unit is not None
            and emitted_unit not in profile.accepted_units
        ):
            continue
        matches.append(profile)
    return matches


def _resolve_input_profile(
    *,
    key: str,
    declared_spec: Any,
    value: Any,
    emitted_unit: str | None,
    signal_type: str | None,
    dtype: str | None,
    shape: tuple[Any, ...] | None,
    schema: dict[str, Any] | None,
    error_cls: type[Exception],
) -> tuple[
    Any, str, str | None, tuple[Any, ...] | None, dict[str, Any] | None, str | None
]:
    inferred_signal_type = signal_type or _infer_signal_type(value)
    matches = _matching_input_profiles(
        declared_spec,
        signal_type=inferred_signal_type,
        dtype=dtype,
        shape=shape,
        schema=schema,
        emitted_unit=emitted_unit,
    )
    if not matches:
        _raise(
            error_cls,
            f"Input '{key}' does not match any accepted input profile; "
            f"got signal_type={inferred_signal_type!r}, dtype={dtype!r}, shape={shape!r}, emitted_unit={emitted_unit!r}",
        )
    if len(matches) != 1:
        _raise(
            error_cls,
            f"Input '{key}' matches multiple accepted input profiles; provide a typed input envelope with "
            "signal_type, dtype, shape, schema, and emitted_unit",
        )

    profile = matches[0]
    resolved_signal_type = signal_type or profile.signal_type or inferred_signal_type
    if resolved_signal_type is None:
        _raise(
            error_cls,
            f"Input '{key}' could not resolve a signal_type from its accepted profiles",
        )

    resolved_dtype = dtype or profile.dtype
    resolved_shape = shape or profile.shape
    resolved_schema = schema or (
        dict(profile.schema) if profile.schema is not None else None
    )
    resolved_unit = emitted_unit
    if profile.accepted_units is not None:
        if resolved_unit is None:
            if len(profile.accepted_units) != 1:
                _raise(
                    error_cls,
                    f"Input '{key}' accepts multiple units {list(profile.accepted_units)} for the matched profile; "
                    "provide a typed input envelope with emitted_unit",
                )
            resolved_unit = profile.accepted_units[0]
        elif resolved_unit not in profile.accepted_units:
            _raise(
                error_cls,
                f"Input '{key}' emitted unit '{resolved_unit}' is not accepted; "
                f"expected one of {list(profile.accepted_units)}",
            )

    return (
        profile,
        resolved_signal_type,
        resolved_dtype,
        resolved_shape,
        resolved_schema,
        resolved_unit,
    )


def _typed_input_signal_spec(
    declared_spec: Any,
    profile: Any,
    *,
    resolved_signal_type: str,
    resolved_dtype: str | None,
    resolved_shape: tuple[Any, ...] | None,
    resolved_schema: dict[str, Any] | None,
    actual_unit: str | None,
):
    from biosim import SignalSpec

    kind = "event" if resolved_signal_type == "event" else declared_spec.kind
    interpolation = (
        "none" if resolved_signal_type == "event" else declared_spec.interpolation
    )
    contract = dict(declared_spec.contract or {})
    if getattr(profile, "contract", None):
        contract = _merge_contract(contract, profile.contract)
    return SignalSpec(
        signal_type=resolved_signal_type,
        kind=kind,
        dtype=resolved_dtype,
        shape=resolved_shape,
        emitted_unit=actual_unit,
        interpolation=interpolation,
        max_age=declared_spec.max_age,
        stale_policy=declared_spec.stale_policy,
        schema=resolved_schema,
        description=declared_spec.description,
        value_type=getattr(declared_spec, "value_type", None),
        format=getattr(declared_spec, "format", None),
        required=getattr(declared_spec, "required", None),
        default=getattr(declared_spec, "default", None),
        advanced=getattr(declared_spec, "advanced", None),
        examples=getattr(declared_spec, "examples", None),
        allowed_values=getattr(declared_spec, "allowed_values", None),
        file=getattr(declared_spec, "file", None),
        ui=getattr(declared_spec, "ui", None),
        contract=contract or None,
    )


def _make_typed_signal(
    *,
    name: str,
    source: str,
    value: Any,
    emitted_at: float,
    declared_spec: Any,
    profile: Any,
    resolved_signal_type: str,
    resolved_dtype: str | None,
    resolved_shape: tuple[Any, ...] | None,
    resolved_schema: dict[str, Any] | None,
    actual_unit: str | None,
    error_cls: type[Exception],
    compatibility_envelope: Any = None,
):
    from biosim import ArraySignal, EventSignal, RecordSignal, ScalarSignal

    signal_spec = _typed_input_signal_spec(
        declared_spec,
        profile,
        resolved_signal_type=resolved_signal_type,
        resolved_dtype=resolved_dtype,
        resolved_shape=resolved_shape,
        resolved_schema=resolved_schema,
        actual_unit=actual_unit,
    )
    signal_types = {
        "scalar": ScalarSignal,
        "array": ArraySignal,
        "record": RecordSignal,
        "event": EventSignal,
    }
    signal_cls = signal_types.get(signal_spec.signal_type)
    if signal_cls is None:
        _raise(
            error_cls,
            f"Unsupported signal_type on declared input port '{name}': {signal_spec.signal_type!r}",
        )
    if compatibility_envelope is not None:
        try:
            compatibility_envelope.validate_contract(signal_spec.contract)
        except ValueError as exc:
            _raise(error_cls, f"Input '{name}': SignalEnvelope doesn't fit this port: {exc}", exc)
    if signal_spec.contract is not None:
        from biosim.compatibility import check_payload, enforce_result

        try:
            enforce_result(
                check_payload(signal_spec.contract, value),
                context=f"Input '{name}'",
            )
        except ValueError as exc:
            _raise(error_cls, str(exc), exc)
    signal = signal_cls(
        source=source, name=name, value=value, emitted_at=emitted_at, spec=signal_spec
    )
    if compatibility_envelope is not None:
        signal.compatibility_envelope = compatibility_envelope
    return signal


def coerce_typed_inputs(
    values: Mapping[str, Any],
    declared_ports: Mapping[str, Any],
    source: str,
    *,
    time_value: float = 0.0,
    error_cls: type[Exception] = RuntimeError,
) -> dict[str, Any]:
    """Coerce raw initial input values into typed BioSignal instances."""

    from biosim import BioSignal, SignalEnvelope

    coerced: dict[str, Any] = {}
    for key, value in values.items():
        declared_spec = declared_ports.get(key)
        if declared_spec is None:
            _raise(error_cls, f"Input '{key}' is not declared by the target module")

        compatibility_envelope = None
        if (
            isinstance(value, Mapping)
            and value.get("schema_version") == "0.1"
            and "contract_digest" in value
        ):
            try:
                compatibility_envelope = SignalEnvelope.from_dict(value)
            except (TypeError, ValueError) as exc:
                _raise(error_cls, f"Input '{key}': invalid SignalEnvelope: {exc}", exc)
            value = compatibility_envelope.payload

        if isinstance(value, BioSignal):
            if (
                value.spec is not None
                and declared_spec.match_input_profile(value.spec) is None
            ):
                actual_unit = (
                    value.spec.emitted_unit if value.spec is not None else None
                )
                _raise(
                    error_cls,
                    f"Input '{key}' emitted profile is not accepted; "
                    f"got signal_type={value.spec.signal_type!r}, dtype={value.spec.dtype!r}, "
                    f"shape={value.spec.shape!r}, emitted_unit={actual_unit!r}",
                )
            coerced[key] = value
            continue
        if isinstance(value, dict) and "spec" in value and "value" in value:
            signal = BioSignal.from_dict(value)
            if (
                signal.spec is None
                or declared_spec.match_input_profile(signal.spec) is None
            ):
                actual_unit = (
                    signal.spec.emitted_unit if signal.spec is not None else None
                )
                _raise(
                    error_cls,
                    f"Input '{key}' emitted profile is not accepted; "
                    f"got signal_type={getattr(signal.spec, 'signal_type', None)!r}, "
                    f"dtype={getattr(signal.spec, 'dtype', None)!r}, "
                    f"shape={getattr(signal.spec, 'shape', None)!r}, emitted_unit={actual_unit!r}",
                )
            coerced[key] = signal
            continue

        actual_unit: str | None = None
        emitted_at = time_value
        signal_value = value
        explicit_signal_type: str | None = None
        explicit_dtype: str | None = None
        explicit_shape: tuple[Any, ...] | None = None
        explicit_schema: dict[str, Any] | None = None
        if isinstance(value, dict) and "value" in value:
            signal_value = value.get("value")
            emitted_at = float(value.get("emitted_at", time_value))
            actual_unit_raw = value.get("emitted_unit", value.get("unit"))
            if actual_unit_raw is not None:
                actual_unit = str(actual_unit_raw)
            explicit_signal_type_raw = value.get("signal_type")
            if explicit_signal_type_raw is not None:
                explicit_signal_type = str(explicit_signal_type_raw)
            explicit_dtype_raw = value.get("dtype")
            if explicit_dtype_raw is not None:
                explicit_dtype = str(explicit_dtype_raw)
            explicit_shape = _normalize_shape_tuple(value.get("shape"))
            explicit_schema = _normalize_schema_dict(value.get("schema"))

        (
            profile,
            resolved_signal_type,
            resolved_dtype,
            resolved_shape,
            resolved_schema,
            actual_unit,
        ) = _resolve_input_profile(
            key=key,
            declared_spec=declared_spec,
            value=signal_value,
            emitted_unit=actual_unit,
            signal_type=explicit_signal_type,
            dtype=explicit_dtype,
            shape=explicit_shape,
            schema=explicit_schema,
            error_cls=error_cls,
        )

        coerced[key] = _make_typed_signal(
            name=key,
            source=source,
            value=signal_value,
            emitted_at=emitted_at,
            declared_spec=declared_spec,
            profile=profile,
            resolved_signal_type=resolved_signal_type,
            resolved_dtype=resolved_dtype,
            resolved_shape=resolved_shape,
            resolved_schema=resolved_schema,
            actual_unit=actual_unit,
            error_cls=error_cls,
            compatibility_envelope=compatibility_envelope,
        )
    return coerced
