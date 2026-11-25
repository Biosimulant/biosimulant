from __future__ import annotations

from types import SimpleNamespace

import pytest

from biosim import signals
from biosim.signals import (
    AcceptedSignalProfile,
    ArraySignal,
    BioSignal,
    EventSignal,
    RecordSignal,
    ScalarSignal,
    SignalSpec,
    coerce_float,
    make_signal,
    unwrap_payload,
)


def test_signal_helper_error_and_fallback_paths(monkeypatch) -> None:
    with pytest.raises(TypeError, match="unexpected"):
        ScalarSignal(source="s", name="x", value=1.0, emitted_at=0.0, extra=True)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="ragged"):
        signals._infer_shape([[1], [1, 2]])
    assert signals._infer_shape(()) == (0,)

    class ArrayLike:
        def tolist(self):
            return (1, 2, 3)

    monkeypatch.setattr(signals, "_HAS_NUMPY", False)
    assert signals._normalize_array_value(ArrayLike(), None) == [1, 2, 3]
    assert signals._normalize_array_value((1, 2), None) == [1, 2]
    with pytest.raises(TypeError, match="array signals"):
        signals._normalize_array_value(1, None)

    with pytest.raises(ValueError, match="max_depth"):
        unwrap_payload({"payload": 1}, max_depth=-1)
    assert unwrap_payload({"payload": {"payload": 2}}, max_depth=2) == 2

    carrier = SimpleNamespace(value={"payload": 7}, emitted_at=0.0)
    assert coerce_float(carrier) == 7.0
    assert coerce_float({"other": "1"}) is None
    assert coerce_float(float("nan")) is None
    assert coerce_float(float("nan"), reject_nan=False) != coerce_float(float("nan"), reject_nan=False)


def test_signal_spec_validation_and_matching_branches() -> None:
    profile = AcceptedSignalProfile(
        signal_type="scalar",
        shape=[],
        accepted_units=["mM"],
    )
    assert profile.shape == ()
    assert profile.accepted_units == ("mM",)

    empty_units = AcceptedSignalProfile(accepted_units=["", "  "])
    assert empty_units.accepted_units is None

    with pytest.raises(ValueError, match="duplicates"):
        AcceptedSignalProfile(accepted_units=("mM", "mM"))
    with pytest.raises(ValueError, match="scalar accepted"):
        AcceptedSignalProfile(signal_type="scalar", shape=(1,))
    with pytest.raises(ValueError, match="array accepted"):
        AcceptedSignalProfile(signal_type="array")
    with pytest.raises(ValueError, match="record accepted"):
        AcceptedSignalProfile(signal_type="record")
    with pytest.raises(ValueError, match="event accepted"):
        AcceptedSignalProfile(signal_type="event", shape=(1,))
    with pytest.raises(ValueError, match="non-negative"):
        AcceptedSignalProfile(shape=(-1,))

    source = SignalSpec.scalar(dtype="float64", emitted_unit="mM")
    assert AcceptedSignalProfile(dtype="int64").matches_output(source) is False
    assert AcceptedSignalProfile(shape=(2,)).matches_output(SignalSpec.array(dtype="float64", shape=(3,))) is False
    assert AcceptedSignalProfile(schema={"x": "float"}).matches_output(SignalSpec.record(schema={"y": "float"})) is False
    assert AcceptedSignalProfile(accepted_units=("nM",)).matches_output(source) is False
    assert AcceptedSignalProfile(accepted_units=("mM",)).matches_output(SignalSpec.scalar()) is False

    spec = SignalSpec.scalar(
        accepted_profiles=[{"signal_type": "record", "schema": {"payload": "json"}}],
        examples=[1],
        allowed_values=[1, 2],
        file={"accept": ".csv"},
        ui={"widget": "slider"},
    )
    assert spec.accepted_profiles and spec.accepted_profiles[0].signal_type == "record"
    assert spec.examples == (1,)
    assert spec.file == {"accept": ".csv"}

    assert SignalSpec.scalar(accepted_profiles=[]).accepted_profiles is None
    with pytest.raises(TypeError, match="AcceptedSignalProfile"):
        SignalSpec.scalar(accepted_profiles=["bad"])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="emitted_unit"):
        SignalSpec.scalar(emitted_unit=" ")
    with pytest.raises(ValueError, match="max_age"):
        SignalSpec.scalar(max_age=-1)
    with pytest.raises(ValueError, match="event signal specs"):
        SignalSpec(signal_type="event")
    with pytest.raises(ValueError, match="non-event"):
        SignalSpec(signal_type="scalar", kind="event")
    with pytest.raises(ValueError, match="scalar signal specs"):
        SignalSpec(signal_type="scalar", shape=(1,))
    with pytest.raises(ValueError, match="array signal specs"):
        SignalSpec(signal_type="array")
    with pytest.raises(ValueError, match="record signal specs"):
        SignalSpec(signal_type="record")
    with pytest.raises(ValueError, match="record signal specs cannot declare shape"):
        SignalSpec(signal_type="record", schema={"x": "float"}, shape=(1,))
    with pytest.raises(ValueError, match="event signal specs cannot declare shape"):
        SignalSpec(signal_type="event", kind="event", interpolation="none", shape=(1,))
    with pytest.raises(ValueError, match="linear interpolation"):
        SignalSpec(signal_type="record", schema={"x": "float"}, interpolation="linear")
    with pytest.raises(ValueError, match="numeric dtype"):
        SignalSpec.scalar(dtype="str", interpolation="linear")
    with pytest.raises(ValueError, match="value_type"):
        SignalSpec.scalar(value_type="unknown")  # type: ignore[arg-type]
    assert SignalSpec.scalar(value_type=" ").value_type is None
    assert SignalSpec.scalar(format=" ").format is None
    with pytest.raises(TypeError, match="examples"):
        SignalSpec(signal_type="scalar", examples={"bad": True})  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="file"):
        SignalSpec(signal_type="scalar", file=["bad"])  # type: ignore[arg-type]


def test_base_signal_paths_and_serialisation(monkeypatch) -> None:
    class BaseScalar(BioSignal):
        signal_type = "scalar"

    class BaseArray(BioSignal):
        signal_type = "array"

    class BaseRecord(BioSignal):
        signal_type = "record"

    class BaseEvent(BioSignal):
        signal_type = "event"

    class UnknownSignal(BioSignal):
        signal_type = "unknown"  # type: ignore[assignment]

    with pytest.raises(TypeError, match="abstract"):
        BioSignal(source="s", name="x", value=1, emitted_at=0.0)
    with pytest.raises(TypeError, match="require source"):
        ScalarSignal(value=1, emitted_at=0.0)

    scalar = BaseScalar("s", "x", 1.0, 0.0, spec=SignalSpec.scalar().to_dict())
    assert scalar.kind == "state"
    assert scalar.with_spec(SignalSpec.scalar(dtype="float32")).spec.dtype == "float32"
    assert scalar.retarget(name="y").name == "y"
    with pytest.raises(TypeError, match="scalar signals"):
        BaseScalar("s", "x", [1], 0.0)

    array_spec = SignalSpec.array(dtype="float64", shape=(2,))
    array = BaseArray("s", "arr", [1.0, 2.0], 0.0, spec=array_spec)
    assert array.to_dict()["value"]["shape"] == [2]
    with pytest.raises(ValueError, match="cannot serialize"):
        BaseArray("s", "arr", [1.0], 0.0)._to_wire_value()
    with pytest.raises(ValueError, match="array signal shape"):
        BaseArray("s", "arr", [1.0], 0.0, spec=array_spec)

    with pytest.raises(TypeError, match="array signals"):
        BaseArray("s", "arr", 1, 0.0)

    record_spec = SignalSpec.record(schema={"x": "float"})
    record = BaseRecord("s", "rec", {"x": 1.0}, 0.0, spec=record_spec)
    assert record.to_dict()["value"] == {"x": 1.0}
    with pytest.raises(TypeError, match="record signals"):
        BaseRecord("s", "rec", 1, 0.0)
    with pytest.raises(ValueError, match="record signal keys"):
        BaseRecord("s", "rec", {"y": 1.0}, 0.0, spec=record_spec)

    event_spec = SignalSpec.event(schema={"x": "float"})
    event = BaseEvent("s", "ev", {"x": 1.0}, 0.0, spec=event_spec)
    assert event.kind == "event"
    assert event.to_dict()["value"] == {"x": 1.0}
    with pytest.raises(TypeError, match="schema-bound event"):
        BaseEvent("s", "ev", 1.0, 0.0, spec=event_spec)
    with pytest.raises(ValueError, match="event signal keys"):
        BaseEvent("s", "ev", {"y": 1.0}, 0.0, spec=event_spec)

    with pytest.raises(ValueError, match="unknown signal type"):
        UnknownSignal("s", "u", 1.0, 0.0)

    with pytest.raises(ValueError, match="unknown signal type"):
        BioSignal.from_dict({"type": "bad", "spec": SignalSpec.scalar().to_dict(), "value": 1})

    assert ScalarSignal("s", "x", 2.5, 0.0, spec=SignalSpec.scalar()).as_float() == 2.5
    with pytest.raises(ValueError, match="not scalar"):
        RecordSignal("s", "r", {"payload": 1}, 0.0, spec=SignalSpec.record(schema={"payload": "json"})).as_float()

    monkeypatch.setattr(signals, "_HAS_NUMPY", False)
    with pytest.raises(ValueError, match="NumPy array adapter"):
        ArraySignal("s", "a", ["hello", "world"], 0.0, spec=array_spec)
    arr = ArraySignal("s", "a", [1.0, 2.0], 0.0)
    assert arr.as_array() == [1.0, 2.0]
    with pytest.raises(ValueError, match="not an array"):
        ScalarSignal("s", "x", 1.0, 0.0, spec=SignalSpec.scalar()).as_array()


def test_concrete_signal_round_trips_and_make_signal_branches() -> None:
    scalar = ScalarSignal("s", "x", 1.0, 0.0, spec=SignalSpec.scalar())
    assert ScalarSignal.from_wire_dict(scalar.to_dict(), spec=scalar.spec).value == 1.0

    array_spec = SignalSpec.array(dtype="float64", shape=(2,))
    array = ArraySignal("s", "a", [1.0, 2.0], 0.0, spec=array_spec)
    assert ArraySignal.from_wire_dict(array.to_dict(), spec=array_spec).value.shape == (2,)
    with pytest.raises(TypeError, match="wire values"):
        ArraySignal.from_wire_dict({"value": [1, 2], "source": "s", "name": "a", "emitted_at": 0}, spec=array_spec)
    with pytest.raises(ValueError, match="cannot serialize"):
        ArraySignal("s", "a", [1.0], 0.0)._to_wire_value()

    record_spec = SignalSpec.record(schema={"x": "float"})
    record = RecordSignal("s", "r", {"x": 1.0}, 0.0, spec=record_spec)
    assert record._clone(name="rr").name == "rr"
    assert RecordSignal.from_wire_dict(record.to_dict(), spec=record_spec).value == {"x": 1.0}
    with pytest.raises(ValueError, match="record signal keys"):
        RecordSignal("s", "r", {"y": 1.0}, 0.0, spec=record_spec)

    event_spec = SignalSpec.event(schema={"payload": "json"})
    event = EventSignal("s", "e", {"payload": 1.0}, 0.0, spec=event_spec)
    assert event._clone(name="ee").name == "ee"
    assert EventSignal.from_wire_dict(event.to_dict(), spec=event_spec).value == {"payload": 1.0}
    with pytest.raises(TypeError, match="schema-bound event"):
        EventSignal("s", "e", 1.0, 0.0, spec=event_spec)

    assert signals._schema_type(True) == "bool"
    assert signals._schema_type(1) == "int"
    assert signals._schema_type(1.2) == "float"
    assert signals._schema_type("x") == "str"
    assert signals._schema_type({"x": 1}) == "json"

    assert make_signal(source="s", name="m", value={"x": 1}, emitted_at=0.0).signal_type == "record"
    assert make_signal(source="s", name="m", value=[1, 2], emitted_at=0.0).value == {"payload": [1, 2]}
    assert make_signal(SignalSpec.array(dtype="float64", shape=(2,)), source="s", name="m", value=[1, 2], emitted_at=0.0).is_array
    assert make_signal(event_spec, source="s", name="m", value=5, emitted_at=0.0).value == {"payload": 5}
    mapped_spec = SignalSpec.scalar().to_dict()
    assert make_signal(mapped_spec, source="s", name="m", value=1, emitted_at=0.0).is_scalar
