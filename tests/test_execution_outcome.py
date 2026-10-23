from pathlib import Path
import pytest
from biosimulant import BioModule, BioWorld, ExecutionOutcome, ExecutionPolicy, SignalSpec
from biosim.execution_outcome import normalize_outcome, aggregate_outcomes

class Adapter(BioModule):
    execution_policy=ExecutionPolicy.ONCE_BEFORE_RUN
    def outputs(self): return {"report":SignalSpec.scalar(dtype="str")}
    def execute(self,inputs,*,context):
        self.report_execution_outcome(ExecutionOutcome.from_returncode(1,stage="annotation",message="Translation mismatch"))
        return {"report":"saved diagnostics"}


def test_child_failure_keeps_outputs_and_survives_snapshot():
    world=BioWorld(communication_step=1);world.add_biomodule('analysis',Adapter());world.run(duration=1)
    outcome=world.execution_outcome()
    assert outcome['state']=='failed'
    assert outcome['stage']=='annotation'
    assert world.get_outputs('analysis')['report'].value=='saved diagnostics'
    saved=world.snapshot()
    world.restore(saved)
    assert world.execution_outcome()==outcome


def test_only_explicit_outcomes_are_interpreted():
    assert normalize_outcome({'status':'failed'}) is None
    assert normalize_outcome({'acceptance':{'status':'failed'}}) is None
    assert aggregate_outcomes({'model':None})['state']=='completed'
    assert aggregate_outcomes({'a':ExecutionOutcome('partial'),'b':ExecutionOutcome('failed')})['state']=='failed'


def test_reported_protocol_is_validated_and_bounded():
    with pytest.raises(ValueError): ExecutionOutcome('research_ready')
    with pytest.raises(ValueError): Adapter().report_execution_outcome({'status':'failed'})
    data=ExecutionOutcome('failed',error={'message':'x','secret':'private'}).to_dict()
    assert data['error']=={'message':'x'}


@pytest.mark.parametrize("invalid", [
    {"schema_version": "1", "state": "unknown"},
    {"schema_version": "1", "state": "failed", "stage": 12},
    {"schema_version": "1", "state": "failed", "error": "not structured"},
    {"schema_version": "1", "state": "failed", "error": {}},
])
def test_invalid_adapter_outcomes_are_rejected(invalid):
    assert normalize_outcome(invalid) is None
    with pytest.raises(ValueError, match="adapter.*invalid execution outcome"):
        aggregate_outcomes({"adapter": invalid})


def test_child_exit_codes_preserve_failure_details_and_bound_retained_errors():
    assert ExecutionOutcome.from_returncode(0).to_dict() == {"schema_version": "1", "state": "completed"}
    failure = ExecutionOutcome.from_returncode(17, stage="child")
    assert failure.to_dict()["error"] == {"code": "command_failed", "message": "Command exited with code 17."}
    for invalid in (True, "1", None):
        with pytest.raises(TypeError, match="integer"):
            ExecutionOutcome.from_returncode(invalid)
    raw = {
        "schema_version": "1", "state": "partial", "stage": "annotation",
        "error": {"message": "x" * 3000, "code": "partial", "record_id": 12, "private": "discard"},
    }
    normalized = normalize_outcome(raw)
    assert len(normalized["error"]["message"]) == 2000
    assert normalized["error"]["record_id"] == "12"
    assert "private" not in normalized["error"]
