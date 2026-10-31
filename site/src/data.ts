export const NONE = 0xffff;

export interface Views {
  layouts: number;
  alphas: number[];
  lo: [number, number];
  hi: [number, number];
  race: string;
  sweeps: { id: string; title: string }[];
}

export interface Meta {
  n: number;
  root: string;
  angles: { id: string; title: string }[];
  sprite: { file: string; tile: number; cols: number; rows: number };
  palette: { slots: string[]; other: string };
  riverMinFlow: number;
  coloredBasins: number;
  views: Views;
}

export interface EmojiNode {
  id: string;
  c: string;
  n: string;
  g: string;
  s: string;
  k: string[];
  x: number;
  y: number;
}

export interface Tree {
  parent: Uint16Array;
  rel: Uint16Array;
}

export interface Link {
  e: string;
  id: string | null;
  r: string;
}

export interface Viewpoint {
  a: string;
  d: string;
  k: string[];
  l: Link[];
}

export interface Base {
  meta: Meta;
  nodes: EmojiNode[];
  relations: string[];
  sprite: HTMLImageElement;
  index: Map<string, number>;
}

async function getJson<T>(path: string): Promise<T> {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return (await r.json()) as T;
}

function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error(`${src}: failed to load`));
    img.src = src;
  });
}

export async function loadBase(): Promise<Base> {
  const meta = await getJson<Meta>("meta.json");
  const [nodes, relations, sprite] = await Promise.all([
    getJson<EmojiNode[]>("nodes.json"),
    getJson<string[]>("relations.json"),
    loadImage(meta.sprite.file),
  ]);
  const index = new Map(nodes.map((node, i) => [node.id, i]));
  return { meta, nodes, relations, sprite, index };
}

class Lru<V> {
  private items = new Map<string, V>();
  private limit: number;

  constructor(limit: number) {
    this.limit = limit;
  }

  get(key: string): V | undefined {
    const v = this.items.get(key);
    if (v !== undefined) {
      this.items.delete(key);
      this.items.set(key, v);
    }
    return v;
  }

  delete(key: string): void {
    this.items.delete(key);
  }

  set(key: string, value: V): void {
    this.items.delete(key);
    this.items.set(key, value);
    while (this.items.size > this.limit) {
      const oldest = this.items.keys().next().value as string;
      this.items.delete(oldest);
    }
  }
}

const trees = new Lru<Promise<Tree>>(64);
const descs = new Lru<Promise<Viewpoint[]>>(64);
const sweeps = new Lru<Promise<Float32Array>>(8);

export function loadTree(id: string, n: number): Promise<Tree> {
  const cached = trees.get(id);
  if (cached) return cached;
  const p = fetch(`tree/${id}.bin`).then(async (r) => {
    if (!r.ok) throw new Error(`tree/${id}.bin: HTTP ${r.status}`);
    const view = new DataView(await r.arrayBuffer());
    if (view.byteLength !== 4 * n) throw new Error(`tree/${id}.bin: expected ${4 * n} bytes, got ${view.byteLength}`);
    const parent = new Uint16Array(n);
    const rel = new Uint16Array(n);
    for (let i = 0; i < n; i++) {
      parent[i] = view.getUint16(2 * i, true);
      rel[i] = view.getUint16(2 * (n + i), true);
    }
    return { parent, rel };
  });
  trees.set(id, p);
  p.catch(() => trees.delete(id));
  return p;
}

export function loadDesc(id: string): Promise<Viewpoint[]> {
  const cached = descs.get(id);
  if (cached) return cached;
  const p = getJson<Viewpoint[]>(`desc/${id}.json`);
  descs.set(id, p);
  p.catch(() => descs.delete(id));
  return p;
}

function decodeSweep(bytes: Uint8Array, meta: Meta): Float32Array {
  const { layouts, lo, hi } = meta.views;
  const stride = meta.n * 2;
  const count = layouts * stride;
  if (bytes.length !== 2 * count) throw new Error(`sweep: expected ${2 * count} bytes, got ${bytes.length}`);
  const out = new Float32Array(count);
  for (let p = 0; p < stride; p++) {
    const axis = p & 1;
    const step = (hi[axis] - lo[axis]) / 65535;
    let level = 0;
    for (let l = 0; l < layouts; l++) {
      const k = l * stride + p;
      level = (level + (bytes[k] | (bytes[count + k] << 8))) & 0xffff;
      out[k] = lo[axis] + level * step;
    }
  }
  return out;
}

export function loadSweep(id: string, meta: Meta): Promise<Float32Array> {
  const cached = sweeps.get(id);
  if (cached) return cached;
  const p = fetch(`views/${id}.bin`).then(async (r) => {
    if (!r.ok || !r.body) throw new Error(`views/${id}.bin: HTTP ${r.status}`);
    const plain = r.body.pipeThrough(new DecompressionStream("gzip"));
    return decodeSweep(new Uint8Array(await new Response(plain).arrayBuffer()), meta);
  });
  sweeps.set(id, p);
  p.catch(() => sweeps.delete(id));
  return p;
}
