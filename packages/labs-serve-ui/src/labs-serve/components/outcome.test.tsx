import { describe, expect, it } from "vitest";
import { compatibilityByWire, scalarOutputs } from "./outcome";
import type { ServeResults } from "../types";

const results: ServeResults = {
  outputs: {
    microbial_growth: {
      viable_cells: { value: 127.4213, spec: { emitted_unit: "cells", description: "Viable cells" } },
      history: { value: [1, 2, 3] },
      label: { value: "steady" },
      single_sample: { value: [42] },
    },
  },
  compatibility: {
    summary: { wires: 2, verified: 1, structural: 1 },
    wires: [
      {
        source: { module: "a", port: "out" },
        target: { module: "b", port: "in" },
        mode: "verified",
      },
      {
        source: { module: "b", port: "out" },
        target: { module: "c", port: "in" },
        mode: "blocked",
      },
    ],
  },
};

describe("scalarOutputs", () => {
  it("keeps the numbers a person can read and skips series and text", () => {
    const outputs = scalarOutputs(results);

    expect(outputs.map((output) => output.key)).toEqual([
      "microbial_growth.viable_cells",
      "microbial_growth.single_sample",
    ]);
    expect(outputs[0].unit).toBe("cells");
    expect(outputs[0].description).toBe("Viable cells");
  });

  it("has nothing to show for results without outputs", () => {
    expect(scalarOutputs({ visuals: [] })).toEqual([]);
    expect(scalarOutputs(null)).toEqual([]);
  });
});

describe("compatibilityByWire", () => {
  it("keys every wire so the canvas can colour it", () => {
    expect(compatibilityByWire(results.compatibility ?? null)).toEqual({
      "a.out->b.in": "verified",
      "b.out->c.in": "blocked",
    });
  });

  it("is empty when a run recorded nothing", () => {
    expect(compatibilityByWire(null)).toEqual({});
  });
});
