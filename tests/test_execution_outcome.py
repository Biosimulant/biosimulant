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
