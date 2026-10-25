import { describe, expect, it } from "vitest";

import { heatmapRange, meaningfulUnit, scaleBarLength, similarityOrder, treeLayout } from "./visual-layout";

// ((a:0.1,b:0.2)95:0.05,c:0.3) with an unrooted-style extra tip d on the root.
const nodes = [
  { id: "n0", label: "n0" },
  { id: "n1", label: "95" },
  { id: "a", label: "Genome A" },
  { id: "b", label: "Genome B" },
  { id: "c", label: "Genome C" },
  { id: "d", label: "Genome D" },
];
const edges = [
  { source: "n0", target: "n1", length: 0.05 },
  { source: "n1", target: "a", length: 0.1 },
  { source: "n1", target: "b", length: 0.2 },
  { source: "n0", target: "c", length: 0.3 },
  { source: "n0", target: "d", length: 0.01 },
];

describe("treeLayout", () => {
  it("keeps every tip and places them by summed branch length", () => {
    const tree = treeLayout(nodes, edges)!;
    expect(tree.leafCount).toBe(4);
    expect(tree.scaled).toBe(true);
    const byId = new Map(tree.nodes.map((node) => [node.id, node]));
    expect(byId.get("b")!.x).toBeCloseTo(0.25);
    expect(tree.maxX).toBeCloseTo(0.3);
    expect(byId.get("n0")!.parent).toBeUndefined();
    expect(byId.get("a")!.parent).toBe("n1");
  });

  it("reads a numeric branch-point label as support and orders small clades first", () => {
    const tree = treeLayout(nodes, edges)!;
    const n1 = tree.nodes.find((node) => node.id === "n1")!;
    expect(n1.support).toBe(95);
    expect(n1.label).toBe("n1");
    // Single tips c and d come before the two-tip clade under n1.
    expect(tree.nodes.filter((node) => node.leaf).map((node) => node.id)).toEqual(["c", "d", "a", "b"]);
    expect(n1.y).toBe(2.5);
  });

  it("draws by depth when branch lengths are missing", () => {
    const tree = treeLayout(nodes, edges.map(({ source, target }) => ({ source, target })), { layout: "tree" })!;
    expect(tree.scaled).toBe(false);
    expect(tree.nodes.find((node) => node.id === "a")!.x).toBe(2);
  });

  it("honours a named root and never roots a fallback on a tip", () => {
    const named = treeLayout(nodes, edges, { root: "n1" })!;
    expect(named.nodes[0].id).toBe("n1");
    // Every edge points away from tip "a", so the fallback would pick it; it moves to n1.
    const reversed = [
      { source: "a", target: "n1", length: 0.1 },
      { source: "n1", target: "b", length: 0.2 },
      { source: "n1", target: "n0", length: 0.05 },
      { source: "n0", target: "c", length: 0.3 },
      { source: "n0", target: "d", length: 0.01 },
    ];
    expect(treeLayout(nodes, reversed)!.nodes[0].id).toBe("n1");
  });

  it("leaves networks, cycles and small plain trees to the network drawing", () => {
    expect(treeLayout(nodes, edges, { layout: "network" })).toBeNull();
    expect(treeLayout(nodes, [...edges.slice(0, 4), { source: "a", target: "b" }])).toBeNull();
    const chain = [{ id: "A", label: "A" }, { id: "B", label: "B" }, { id: "C", label: "C" }];
    const chainEdges = [{ source: "A", target: "B" }, { source: "B", target: "C" }];
    expect(treeLayout(chain, chainEdges)).toBeNull();
    expect(treeLayout(chain, chainEdges, { layout: "tree" })).not.toBeNull();
  });

  it("draws a large plain tree as a tree without being asked", () => {
    const many = Array.from({ length: 41 }, (_, index) => ({ id: `t${index}`, label: `t${index}` }));
    const star = many.slice(1).map((node) => ({ source: "t0", target: node.id }));
    expect(treeLayout(many, star)!.leafCount).toBe(40);
  });
});

describe("similarityOrder", () => {
  // a and c are close, b and d are close.
  const distances = [
    [0, 0.9, 0.1, 0.8],
    [0.9, 0, 0.85, 0.2],
    [0.1, 0.85, 0, 0.9],
    [0.8, 0.2, 0.9, 0],
  ];

  it("groups close rows of a distance matrix", () => {
    expect(similarityOrder(distances)).toEqual([0, 2, 1, 3]);
  });

  it("treats a matrix with its largest values on the diagonal as similarities", () => {
    const similarities = distances.map((row) => row.map((value) => 1 - value));
    expect(similarityOrder(similarities)).toEqual([0, 2, 1, 3]);
  });

  it("leaves non-square, asymmetric or mislabelled matrices alone", () => {
    expect(similarityOrder([[0, 1, 2], [1, 0, 3]])).toBeNull();
    expect(similarityOrder([[0, 1, 2], [5, 0, 3], [2, 3, 0]])).toBeNull();
    expect(similarityOrder(distances, ["a", "b", "c", "d"], ["a", "b", "d", "c"])).toBeNull();
    expect(similarityOrder([[0, 1, 2], [1, 0, 1], [2, 1, 0]])).toBeNull();
  });
});

describe("heatmap helpers", () => {
  it("uses a valid fixed range and otherwise the data extent", () => {
    expect(heatmapRange([0, 1], [0.001, 0.0117])).toEqual({ min: 0, max: 1, fixed: true });
    expect(heatmapRange([1, 0], [0.001, 0.0117])).toEqual({ min: 0.001, max: 0.0117, fixed: false });
    expect(heatmapRange(undefined, [Number.NaN])).toEqual({ min: 0, max: 1, fixed: false });
  });

  it("drops units that say nothing", () => {
    expect(meaningfulUnit("1")).toBeUndefined();
    expect(meaningfulUnit(" Dimensionless ")).toBeUndefined();
    expect(meaningfulUnit("CFU")).toBe("CFU");
  });

  it("picks a round scale bar", () => {
    expect(scaleBarLength(0.3)).toBeCloseTo(0.1);
    expect(scaleBarLength(0.0117)).toBeCloseTo(0.005);
    expect(scaleBarLength(0)).toBe(0);
  });
});
