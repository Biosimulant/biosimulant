from biosim import BioModule, ExecutionContext, ExecutionPolicy, SignalSpec


class Sink(BioModule):
    execution_policy = ExecutionPolicy.ONCE_BEFORE_RUN

    def inputs(self):
        return {"protein_sequence": SignalSpec.scalar(dtype="str", format="sequence")}

    def outputs(self):
        return {"received": SignalSpec.scalar(dtype="float64")}

    def execute(self, inputs, *, context: ExecutionContext):
        return {"received": 1.0 if "protein_sequence" in inputs else 0.0}
