from biosim import BioModule, ExecutionContext, ExecutionPolicy, SignalSpec


class Consumer(BioModule):
    execution_policy = ExecutionPolicy.ONCE_BEFORE_RUN

    def inputs(self):
        # The protein.sequence/v1 contract is declared only in model.yaml.
        return {
            "protein_sequence": SignalSpec.scalar(dtype="str", format="sequence"),
            "note": SignalSpec.scalar(dtype="str"),
        }

    def outputs(self):
        return {"length": SignalSpec.scalar(dtype="float64")}

    def execute(self, inputs, *, context: ExecutionContext):
        sequence = inputs.get("protein_sequence")
        return {"length": float(len(sequence.value)) if sequence is not None else 0.0}
