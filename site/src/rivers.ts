import type { Meta } from "./data.ts";
import type { Drainage } from "./tree.ts";

export interface Curve {
  u: number;
  ax: number;
  ay: number;
  c1x: number;
  c1y: number;
  c2x: number;
  c2y: number;
  bx: number;
  by: number;
}

function unit(x: number, y: number): [number, number] {
  const l = Math.hypot(x, y);
  return l > 1e-12 ? [x / l, y / l] : [0, 0];
}

export function stepCurve(d: Drainage, xs: Float64Array, ys: Float64Array, u: number): Curve {
  const p = d.parent[u];
  const ax = xs[u];
  const ay = ys[u];
  const bx = xs[p];
  const by = ys[p];
  const length = Math.hypot(bx - ax, by - ay);
  const [sx, sy] = unit(bx - ax, by - ay);
  const [dx, dy] = p === d.root ? [sx, sy] : unit(xs[d.parent[p]] - bx, ys[d.parent[p]] - by);
  const [rx, ry] = unit(sx + dx, sy + dy);
  return {
    u,
    ax,
    ay,
    c1x: ax + sx * length * 0.33,
    c1y: ay + sy * length * 0.33,
    c2x: bx - rx * length * 0.33,
    c2y: by - ry * length * 0.33,
    bx,
    by,
  };
}

export function riverOrder(d: Drainage, minFlow: number): number[] {
  const out: number[] = [];
  for (let u = 0; u < d.parent.length; u++) {
    if (u !== d.root && d.flow[u] >= minFlow) out.push(u);
  }
  return out.sort((a, b) => d.flow[a] - d.flow[b] || a - b);
}

export interface Palette {
  color: (i: number) => string;
  alpha: (i: number) => number;
  width: (i: number) => number;
  headColor: (head: number) => string;
}

export function palette(d: Drainage, meta: Meta): Palette {
  const rank = new Map(d.heads.map((h, k) => [h, k]));
  const colored = Math.min(meta.coloredBasins, meta.palette.slots.length);
  const maxFlow = Math.max(1, ...d.heads.map((h) => d.flow[h]));
  const headColor = (h: number) => {
    const k = rank.get(h) ?? Infinity;
    return k < colored ? meta.palette.slots[k] : meta.palette.other;
  };
  return {
    headColor,
    color: (i) => (i === d.root ? "#0b0b0b" : headColor(d.basin[i])),
    alpha: (i) => ((rank.get(d.basin[i]) ?? Infinity) < colored ? 0.85 : 0.5),
    width: (i) => 0.5 + 8 * Math.sqrt(d.flow[i] / maxFlow),
  };
}
