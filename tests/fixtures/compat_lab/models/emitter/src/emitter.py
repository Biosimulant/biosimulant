from biosim import BioModule, ExecutionContext, ExecutionPolicy, SignalSpec


class Emitter(BioModule):
    execution_policy = ExecutionPolicy.ONCE_BEFORE_RUN

    def __init__(self, sequence: str = "MKTAYIAKQR", note: str = "fixture"):
        self.sequence = sequence
        self.note = note

    def outputs(self):
        return {
            "protein_sequence": SignalSpec.scalar(dtype="str", format="sequence"),
            "note": SignalSpec.scalar(dtype="str"),
        }

    def execute(self, inputs, *, context: ExecutionContext):
        return {"protein_sequence": self.sequence, "note": self.note}
