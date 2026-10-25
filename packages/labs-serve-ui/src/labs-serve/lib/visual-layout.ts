/**
 * Layout for tree-shaped graphs and row order for symmetric matrices.
 *
 * Pure functions with no DOM. Biosimulant Studio keeps a copy of this file for
 * its run charts and PDF export, so a tree or distance matrix is drawn the same
 * way locally, on the web and on paper. Change both copies together.
 */

export type LayoutNode = { id: string; label: string; support?: number };
export type LayoutEdge = { source: string; target: string; length?: number };

export type TreeLayoutNode = {
  id: string;
  label: string;
  /** Distance from the root: summed branch lengths, or depth when lengths are missing. */
  x: number;
  /** Tips sit on rows 0, 1, 2…; a branch point sits midway between its outermost children. */
  y: number;
  parent?: string;
  leaf: boolean;
  support?: number;
};

export type TreeLayout = {
  /** Every node, parents before their children. */
  nodes: TreeLayoutNode[];
  leafCount: number;
  maxX: number;
  /** True when every edge has a branch length, so x is a distance rather than a depth. */
  scaled: boolean;
};

const NUMERIC = /^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$/;
// Without `layout: "tree"`, a tree this large is drawn as one anyway: a
// network drawing of it would have to drop nodes, and tips first.
const AUTO_TREE_NODES = 40;

/**
 * Lays a graph out as a rectangular tree, or returns null when it should be
 * drawn as a network: it is not one connected tree, the producer asked for a
 * network, or it is a small tree with nothing tree-specific about it (no
 * `layout: "tree"`, branch lengths or support values).
 *
 * Children are ordered smallest clade first, as most tree viewers do, so the
 * drawing reads as a ladder rather than a tangle. A numeric label on a branch
 * point is read as its support value, which is how Newick stores bootstrap.
 */
export function treeLayout(
  nodes: LayoutNode[],
  edges: LayoutEdge[],
  options: { layout?: unknown; root?: unknown } = {},
): TreeLayout | null {
  if (options.layout === "network") return null;
  if (nodes.length < 2 || edges.length !== nodes.length - 1) return null;
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const neighbours = new Map<string, Array<{ id: string; length?: number }>>(nodes.map((node) => [node.id, []]));
  for (const edge of edges) {
    const from = neighbours.get(edge.source);
    const to = neighbours.get(edge.target);
    if (!from || !to || edge.source === edge.target) return null;
    from.push({ id: edge.target, length: edge.length });
    to.push({ id: edge.source, length: edge.length });
  }

  const root = treeRoot(nodes, edges, neighbours, options.root);
  const parent = new Map<string, string>();
  const branch = new Map<string, number | undefined>();
  const children = new Map<string, string[]>();
  const order = [root];
  const seen = new Set([root]);
  for (let index = 0; index < order.length; index += 1) {
    const id = order[index];
    const kids: string[] = [];
    for (const next of neighbours.get(id)!) {
      if (seen.has(next.id)) continue;
      seen.add(next.id);
      parent.set(next.id, id);
      branch.set(next.id, next.length);
      kids.push(next.id);
      order.push(next.id);
    }
    children.set(id, kids);
  }
  if (seen.size !== nodes.length) return null;

  const tips = new Map<string, number>();
  for (const id of [...order].reverse()) {
    const kids = children.get(id)!;
    tips.set(id, kids.length === 0 ? 1 : kids.reduce((sum, kid) => sum + tips.get(kid)!, 0));
    kids.sort((a, b) => tips.get(a)! - tips.get(b)!);
  }

  const lengths = [...branch.values()];
  const scaled = lengths.every((length) => typeof length === "number" && Number.isFinite(length) && length >= 0)
    && lengths.some((length) => (length ?? 0) > 0);
  const x = new Map<string, number>([[root, 0]]);
  const y = new Map<string, number>();
  const preorder: string[] = [];
  const stack = [root];
  let row = 0;
  while (stack.length > 0) {
    const id = stack.pop()!;
    preorder.push(id);
    const kids = children.get(id)!;
    if (kids.length === 0) y.set(id, row++);
    for (const kid of kids) x.set(kid, x.get(id)! + (scaled ? branch.get(kid)! : 1));
    for (let index = kids.length - 1; index >= 0; index -= 1) stack.push(kids[index]);
  }
  for (const id of [...preorder].reverse()) {
    const kids = children.get(id)!;
    if (kids.length > 0) y.set(id, (y.get(kids[0])! + y.get(kids[kids.length - 1])!) / 2);
  }

  const laid = preorder.map((id): TreeLayoutNode => {
    const node = byId.get(id)!;
    const leaf = children.get(id)!.length === 0;
    const numericLabel = !leaf && node.label !== node.id && NUMERIC.test(node.label.trim());
    return {
      id,
      label: numericLabel ? node.id : node.label,
      x: x.get(id)!,
      y: y.get(id)!,
      parent: parent.get(id),
      leaf,
      support: node.support ?? (numericLabel ? Number(node.label) : undefined),
    };
  });
  const treeLike = options.layout === "tree" || scaled || nodes.length > AUTO_TREE_NODES
    || laid.some((node) => node.support !== undefined);
  if (!treeLike) return null;
  return { nodes: laid, leafCount: row, maxX: Math.max(...laid.map((node) => node.x)), scaled };
}

/**
 * The named root; else the one node no edge points to; else the first node.
 * A fallback that lands on a tip moves to its neighbour, so an unrooted tree
 * is not drawn hanging from one of its own leaves.
 */
function treeRoot(
  nodes: LayoutNode[],
  edges: LayoutEdge[],
  neighbours: Map<string, Array<{ id: string }>>,
  named: unknown,
): string {
  if (typeof named === "string" && neighbours.has(named)) return named;
  const targets = new Set(edges.map((edge) => edge.target));
  const sources = nodes.filter((node) => !targets.has(node.id));
  const root = sources.length === 1 ? sources[0].id : nodes[0].id;
  const adjacent = neighbours.get(root)!;
  return adjacent.length === 1 ? adjacent[0].id : root;
}

/**
 * A row order that puts similar rows next to each other, for a square
 * symmetric matrix such as pairwise distances. Rows are grouped by average
 * linkage (UPGMA); the diagonal decides whether high values mean far apart
 * (distances, smallest on the diagonal) or close together (similarities).
 *
 * Returns null when the matrix is not one this applies to, or when grouping
 * would leave the order unchanged.
 */
export function similarityOrder(values: number[][], xLabels: string[] = [], yLabels: string[] = []): number[] | null {
  const n = values.length;
  if (n < 3 || values.some((row) => row.length !== n || row.some((value) => !Number.isFinite(value)))) return null;
  if (xLabels.length > 0 && yLabels.length > 0 && xLabels.some((label, index) => label !== yLabels[index])) return null;
  for (let i = 0; i < n; i += 1) {
    for (let j = i + 1; j < n; j += 1) {
      const a = values[i][j];
      const b = values[j][i];
      if (Math.abs(a - b) > 1e-9 * Math.max(1, Math.abs(a), Math.abs(b))) return null;
    }
  }
  const offDiagonal = (i: number) => values[i].filter((_, j) => j !== i);
  const distanceLike = values.every((row, i) => row[i] <= Math.min(...offDiagonal(i)));
  const similarityLike = values.every((row, i) => row[i] >= Math.max(...offDiagonal(i)));
  if (distanceLike === similarityLike) return null;
  const top = Math.max(...values.flat());
  const distance = (i: number, j: number) => (distanceLike ? values[i][j] : top - values[i][j]);

  type Cluster = { members: number[]; order: number[] };
  const clusters: Cluster[] = Array.from({ length: n }, (_, index) => ({ members: [index], order: [index] }));
  const linkage = (a: Cluster, b: Cluster) => {
    let sum = 0;
    for (const i of a.members) for (const j of b.members) sum += distance(i, j);
    return sum / (a.members.length * b.members.length);
  };
  while (clusters.length > 1) {
    let best = [0, 1];
    let bestDistance = Number.POSITIVE_INFINITY;
    for (let i = 0; i < clusters.length; i += 1) {
      for (let j = i + 1; j < clusters.length; j += 1) {
        const d = linkage(clusters[i], clusters[j]);
        if (d < bestDistance) {
          bestDistance = d;
          best = [i, j];
        }
      }
    }
    const [i, j] = best;
    const merged = { members: [...clusters[i].members, ...clusters[j].members], order: [...clusters[i].order, ...clusters[j].order] };
    clusters.splice(j, 1);
    clusters.splice(i, 1, merged);
  }
  const order = clusters[0].order;
  return order.every((row, index) => row === index) ? null : order;
}

/** A unit worth printing: "1", "dimensionless" and "unitless" say nothing to a reader. */
export function meaningfulUnit(unit: string | undefined): string | undefined {
  const trimmed = unit?.trim();
  if (!trimmed || ["1", "dimensionless", "unitless", "none"].includes(trimmed.toLowerCase())) return undefined;
  return trimmed;
}

/** The heatmap's colour range: the producer's fixed `range` when valid, else the data's extent. */
export function heatmapRange(range: unknown, values: number[]): { min: number; max: number; fixed: boolean } {
  if (Array.isArray(range) && range.length === 2) {
    const [low, high] = range.map(Number);
    if (Number.isFinite(low) && Number.isFinite(high) && low < high) return { min: low, max: high, fixed: true };
  }
  const finite = values.filter((value) => Number.isFinite(value));
  return finite.length > 0
    ? { min: Math.min(...finite), max: Math.max(...finite), fixed: false }
    : { min: 0, max: 1, fixed: false };
}

/** A round branch length about a fifth of the tree's depth, for the scale bar. */
export function scaleBarLength(maxX: number): number {
  if (!(maxX > 0)) return 0;
  const target = maxX / 5;
  const power = 10 ** Math.floor(Math.log10(target));
  const step = [1, 2, 5, 10].find((multiple) => multiple * power >= target) ?? 10;
  return step * power;
}
