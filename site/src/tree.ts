import { NONE, type Tree } from "./data.ts";

export interface Drainage {
  root: number;
  parent: Uint16Array;
  rel: Uint16Array;
  children: number[][];
  flow: Uint32Array;
  hops: Uint16Array;
  basin: Int32Array;
  heads: number[];
  maxHops: number;
}

export interface Step {
  from: number;
  to: number;
  rel: number;
}

export function drainage(tree: Tree): Drainage {
  const n = tree.parent.length;
  const children: number[][] = Array.from({ length: n }, () => []);
  let root = -1;
  for (let i = 0; i < n; i++) {
    const p = tree.parent[i];
    if (p === NONE) root = i;
    else children[p].push(i);
  }
  if (root < 0) throw new Error("tree has no root");
  const order = [root];
  const hops = new Uint16Array(n);
  for (let k = 0; k < order.length; k++) {
    const i = order[k];
    for (const c of children[i]) {
      hops[c] = hops[i] + 1;
      order.push(c);
    }
  }
  if (order.length !== n) throw new Error(`tree reaches ${order.length} of ${n} emoji`);
  const flow = new Uint32Array(n).fill(1);
  for (let k = order.length - 1; k > 0; k--) flow[tree.parent[order[k]]] += flow[order[k]];
  const basin = new Int32Array(n).fill(-1);
  for (const k of order) {
    if (k === root) continue;
    const p = tree.parent[k];
    basin[k] = p === root ? k : basin[p];
  }
  const heads = [...children[root]].sort((a, b) => flow[b] - flow[a] || a - b);
  let maxHops = 0;
  for (let i = 0; i < n; i++) maxHops = Math.max(maxHops, hops[i]);
  return { root, parent: tree.parent, rel: tree.rel, children, flow, hops, basin, heads, maxHops };
}

export function chain(d: Drainage, from: number): Step[] {
  const steps: Step[] = [];
  let i = from;
  while (i !== d.root) {
    const p = d.parent[i];
    steps.push({ from: i, to: p, rel: d.rel[i] });
    i = p;
  }
  return steps;
}
