import type { Base } from "./data.ts";
import { chain, type Drainage } from "./tree.ts";
import { palette, riverOrder, stepCurve, type Curve, type Palette } from "./rivers.ts";

export interface MapEvents {
  onFocus(i: number): void;
  onPin(i: number | null): void;
  onHover(i: number | null): void;
}

const SURFACE = "#fcfcfb";
const INK = "#0b0b0b";
const CELL = 0.02;
const LANDMARK_SPACING = 34;

interface Drag {
  x: number;
  y: number;
  tx: number;
  ty: number;
  moved: boolean;
}

export class MapView {
  private canvas: HTMLCanvasElement;
  private base: Base;
  private events: MapEvents;
  private ctx: CanvasRenderingContext2D;
  private xs: Float64Array;
  private ys: Float64Array;
  private grid = new Map<number, number[]>();
  private d: Drainage | null = null;
  private pal: Palette | null = null;
  private curves: Curve[] = [];
  private riverUs: number[] = [];
  private byFlow: number[] = [];
  private rivers = 1;
  private indexed = false;
  private mouse: [number, number] | null = null;
  private hover: number | null = null;
  private pinned: number | null = null;
  private scale = 1;
  private fitScale = 1;
  private tx = 0;
  private ty = 0;
  private w = 0;
  private h = 0;
  private dpr = 1;
  private fitted = false;
  private queued = false;
  private pointers = new Map<number, { x: number; y: number }>();
  private drag: Drag | null = null;
  private pinch: { dist: number; mx: number; my: number } | null = null;

  constructor(canvas: HTMLCanvasElement, base: Base, events: MapEvents) {
    this.canvas = canvas;
    this.base = base;
    this.events = events;
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("canvas 2d context unavailable");
    this.ctx = ctx;
    const n = base.nodes.length;
    this.xs = new Float64Array(n);
    this.ys = new Float64Array(n);
    base.nodes.forEach((node, i) => {
      this.xs[i] = node.x;
      this.ys[i] = node.y;
    });
    this.index();
    new ResizeObserver(() => this.resize()).observe(canvas.parentElement ?? canvas);
    canvas.addEventListener("pointerdown", (e) => this.pointerDown(e));
    canvas.addEventListener("pointermove", (e) => this.pointerMove(e));
    canvas.addEventListener("pointerup", (e) => this.pointerUp(e));
    canvas.addEventListener("pointercancel", (e) => this.pointerUp(e, true));
    canvas.addEventListener("pointerleave", () => {
      this.mouse = null;
      this.setHover(null);
    });
    canvas.addEventListener("wheel", (e) => this.wheel(e), { passive: false });
    this.resize();
  }

  setDrainage(d: Drainage): void {
    this.d = d;
    this.pal = palette(d, this.base.meta);
    this.riverUs = riverOrder(d, this.base.meta.riverMinFlow);
    this.curves = this.riverUs.map((u) => stepCurve(d, this.xs, this.ys, u));
    this.byFlow = Array.from(d.flow.keys()).sort((a, b) => d.flow[b] - d.flow[a] || a - b);
    this.ensureVisible(d.root);
    this.invalidate();
  }

  setPositions(xs: Float64Array, ys: Float64Array, rivers: number): void {
    this.xs.set(xs);
    this.ys.set(ys);
    this.rivers = rivers;
    this.indexed = false;
    const d = this.d;
    if (d) this.curves = this.riverUs.map((u) => stepCurve(d, this.xs, this.ys, u));
    if (this.mouse) this.setHover(this.hit(this.mouse[0], this.mouse[1]));
    this.invalidate();
  }

  revealFocus(): void {
    if (!this.d) return;
    this.ensureVisible(this.d.root);
    this.invalidate();
  }

  private index(): void {
    this.indexed = true;
    this.grid.clear();
    for (let i = 0; i < this.xs.length; i++) {
      const key = this.cellKey(Math.floor(this.xs[i] / CELL), Math.floor(this.ys[i] / CELL));
      const bucket = this.grid.get(key);
      if (bucket) bucket.push(i);
      else this.grid.set(key, [i]);
    }
  }

  setPinned(i: number | null): void {
    this.pinned = i;
    this.invalidate();
  }

  get palette(): Palette | null {
    return this.pal;
  }

  private cellKey(cx: number, cy: number): number {
    return cx * 100003 + cy;
  }

  private resize(): void {
    const rect = this.canvas.getBoundingClientRect();
    this.dpr = window.devicePixelRatio || 1;
    this.w = rect.width;
    this.h = rect.height;
    this.canvas.width = Math.round(rect.width * this.dpr);
    this.canvas.height = Math.round(rect.height * this.dpr);
    this.fitScale = Math.min(this.w, this.h) * 0.94;
    if (!this.fitted && this.w > 0 && this.h > 0) {
      this.scale = this.fitScale;
      this.tx = (this.w - this.scale) / 2;
      this.ty = (this.h - this.scale) / 2;
      this.fitted = true;
    }
    this.invalidate();
  }

  private ensureVisible(i: number): void {
    const [sx, sy] = this.toScreen(i);
    const margin = 60;
    if (sx < margin || sy < margin || sx > this.w - margin || sy > this.h - margin) {
      this.tx += this.w / 2 - sx;
      this.ty += this.h / 2 - sy;
    }
  }

  private toScreen(i: number): [number, number] {
    return [this.xs[i] * this.scale + this.tx, this.ys[i] * this.scale + this.ty];
  }

  private invalidate(): void {
    if (this.queued) return;
    this.queued = true;
    requestAnimationFrame(() => {
      this.queued = false;
      this.draw();
    });
  }

  private spriteSize(): number {
    return Math.min(64, this.scale * 0.016);
  }

  private hit(px: number, py: number): number | null {
    if (!this.indexed) this.index();
    const radius = Math.max(10, this.spriteSize() / 2) / this.scale;
    const mx = (px - this.tx) / this.scale;
    const my = (py - this.ty) / this.scale;
    const span = Math.ceil(radius / CELL);
    const cx = Math.floor(mx / CELL);
    const cy = Math.floor(my / CELL);
    const stacked = this.spriteSize() >= 14 && this.d !== null;
    let best: number | null = null;
    let bestDist = radius;
    let bestFlow = -1;
    for (let gx = cx - span; gx <= cx + span; gx++) {
      for (let gy = cy - span; gy <= cy + span; gy++) {
        for (const i of this.grid.get(this.cellKey(gx, gy)) ?? []) {
          const dx = Math.abs(this.xs[i] - mx);
          const dy = Math.abs(this.ys[i] - my);
          if (stacked && this.d) {
            const flow = i === this.d.root ? Infinity : this.d.flow[i];
            if (dx <= radius && dy <= radius && flow > bestFlow) {
              bestFlow = flow;
              best = i;
            }
          } else {
            const dist = Math.hypot(dx, dy);
            if (dist <= bestDist) {
              bestDist = dist;
              best = i;
            }
          }
        }
      }
    }
    return best;
  }

  private setHover(i: number | null): void {
    if (i === this.hover) return;
    this.hover = i;
    this.canvas.style.cursor = i === null ? "grab" : "pointer";
    this.events.onHover(i);
    this.invalidate();
  }

  private zoomAt(px: number, py: number, factor: number): void {
    const next = Math.min(this.fitScale * 40, Math.max(this.fitScale * 0.5, this.scale * factor));
    const k = next / this.scale;
    this.tx = px - (px - this.tx) * k;
    this.ty = py - (py - this.ty) * k;
    this.scale = next;
    this.invalidate();
  }

  private local(e: PointerEvent | WheelEvent): [number, number] {
    const rect = this.canvas.getBoundingClientRect();
    return [e.clientX - rect.left, e.clientY - rect.top];
  }

  private grip(): { dist: number; mx: number; my: number } {
    const [a, b] = [...this.pointers.values()];
    return { dist: Math.hypot(a.x - b.x, a.y - b.y), mx: (a.x + b.x) / 2, my: (a.y + b.y) / 2 };
  }

  private pointerDown(e: PointerEvent): void {
    this.canvas.setPointerCapture(e.pointerId);
    const [x, y] = this.local(e);
    this.pointers.set(e.pointerId, { x, y });
    if (this.pointers.size === 1) {
      this.drag = { x, y, tx: this.tx, ty: this.ty, moved: false };
    } else {
      this.pinch = this.grip();
      if (this.drag) this.drag.moved = true;
    }
  }

  private pointerMove(e: PointerEvent): void {
    const [x, y] = this.local(e);
    if (!this.pointers.has(e.pointerId)) {
      if (e.pointerType === "mouse") {
        this.mouse = [x, y];
        this.setHover(this.hit(x, y));
      }
      return;
    }
    this.pointers.set(e.pointerId, { x, y });
    if (this.pinch && this.pointers.size >= 2) {
      const now = this.grip();
      this.tx += now.mx - this.pinch.mx;
      this.ty += now.my - this.pinch.my;
      if (this.pinch.dist > 0 && now.dist > 0) this.zoomAt(now.mx, now.my, now.dist / this.pinch.dist);
      else this.invalidate();
      this.pinch = now;
      return;
    }
    if (this.drag) {
      const dx = x - this.drag.x;
      const dy = y - this.drag.y;
      if (Math.hypot(dx, dy) > 4) this.drag.moved = true;
      if (this.drag.moved) {
        this.tx = this.drag.tx + dx;
        this.ty = this.drag.ty + dy;
        this.canvas.style.cursor = "grabbing";
        this.invalidate();
      }
    }
  }

  private pointerUp(e: PointerEvent, cancelled = false): void {
    if (!this.pointers.has(e.pointerId)) return;
    const [x, y] = this.local(e);
    const wasClick = !cancelled && this.pointers.size === 1 && this.drag !== null && !this.drag.moved;
    this.pointers.delete(e.pointerId);
    if (this.pointers.size >= 2) {
      this.pinch = this.grip();
    } else if (this.pointers.size === 1) {
      this.pinch = null;
      const [rest] = [...this.pointers.values()];
      this.drag = { x: rest.x, y: rest.y, tx: this.tx, ty: this.ty, moved: true };
    } else {
      this.pinch = null;
      this.drag = null;
    }
    this.canvas.style.cursor = this.hover === null ? "grab" : "pointer";
    if (!wasClick) return;
    const i = this.hit(x, y);
    if (i === null) {
      this.events.onPin(null);
    } else if (e.pointerType === "mouse") {
      if (e.shiftKey) this.events.onPin(i);
      else this.events.onFocus(i);
    } else if (i === this.pinned) {
      this.events.onFocus(i);
    } else {
      this.events.onPin(i);
    }
  }

  private wheel(e: WheelEvent): void {
    e.preventDefault();
    const [x, y] = this.local(e);
    this.zoomAt(x, y, Math.exp(-e.deltaY * 0.0015));
  }

  private sprite(i: number, cx: number, cy: number, size: number): void {
    const { tile, cols } = this.base.meta.sprite;
    this.ctx.drawImage(this.base.sprite, (i % cols) * tile, Math.floor(i / cols) * tile, tile, tile, cx - size / 2, cy - size / 2, size, size);
  }

  private curve(c: Curve): void {
    const s = this.scale;
    const ctx = this.ctx;
    ctx.beginPath();
    ctx.moveTo(c.ax * s + this.tx, c.ay * s + this.ty);
    ctx.bezierCurveTo(c.c1x * s + this.tx, c.c1y * s + this.ty, c.c2x * s + this.tx, c.c2y * s + this.ty, c.bx * s + this.tx, c.by * s + this.ty);
    ctx.stroke();
  }

  private visible(sx: number, sy: number, pad: number): boolean {
    return sx > -pad && sy > -pad && sx < this.w + pad && sy < this.h + pad;
  }

  private draw(): void {
    const ctx = this.ctx;
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.fillStyle = SURFACE;
    ctx.fillRect(0, 0, this.w, this.h);
    const d = this.d;
    const pal = this.pal;
    if (!d || !pal) return;
    const active = this.hover ?? this.pinned;
    const path = active !== null && active !== d.root ? chain(d, active) : [];
    const dim = path.length ? 0.3 : 1;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    for (const c of this.curves) {
      ctx.strokeStyle = pal.color(c.u);
      ctx.globalAlpha = pal.alpha(c.u) * dim * this.rivers;
      ctx.lineWidth = pal.width(c.u);
      this.curve(c);
    }
    ctx.globalAlpha = 0.75 * dim;
    for (let i = 0; i < this.xs.length; i++) {
      const [sx, sy] = this.toScreen(i);
      if (!this.visible(sx, sy, 4)) continue;
      ctx.fillStyle = pal.color(i);
      ctx.fillRect(sx - 1.6, sy - 1.6, 3.2, 3.2);
    }
    const size = this.spriteSize();
    const onPath = new Set(path.map((s) => s.from));
    ctx.globalAlpha = dim;
    if (size >= 14) {
      for (let k = this.byFlow.length - 1; k >= 0; k--) {
        const i = this.byFlow[k];
        if (i === d.root || onPath.has(i)) continue;
        const [sx, sy] = this.toScreen(i);
        if (this.visible(sx, sy, size)) this.sprite(i, sx, sy, size);
      }
    } else {
      const placed: [number, number][] = [this.toScreen(d.root)];
      for (const i of this.byFlow.slice(0, 300)) {
        if (i === d.root || onPath.has(i)) continue;
        const [sx, sy] = this.toScreen(i);
        if (!this.visible(sx, sy, 26)) continue;
        if (placed.every(([px, py]) => Math.hypot(px - sx, py - sy) >= LANDMARK_SPACING)) {
          placed.push([sx, sy]);
          this.sprite(i, sx, sy, 24);
        }
      }
    }
    ctx.globalAlpha = 1;
    if (path.length) {
      const curves = path.map((step) => stepCurve(d, this.xs, this.ys, step.from));
      ctx.strokeStyle = SURFACE;
      ctx.lineWidth = 8;
      for (const c of curves) this.curve(c);
      ctx.strokeStyle = INK;
      ctx.lineWidth = 3.5;
      for (const c of curves) this.curve(c);
    }
    const pathSize = Math.max(size, 28);
    for (const i of onPath) {
      const [sx, sy] = this.toScreen(i);
      const s = i === active ? pathSize * 1.3 : pathSize;
      ctx.fillStyle = SURFACE;
      ctx.beginPath();
      ctx.arc(sx, sy, s * 0.6, 0, Math.PI * 2);
      ctx.fill();
      this.sprite(i, sx, sy, s);
    }
    const [rx, ry] = this.toScreen(d.root);
    const rootSize = Math.max(size * 1.6, 44);
    ctx.fillStyle = SURFACE;
    ctx.strokeStyle = INK;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(rx, ry, rootSize * 0.62, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    this.sprite(d.root, rx, ry, rootSize);
    if (this.hover !== null && this.hover !== d.root) {
      const [hx, hy] = this.toScreen(this.hover);
      ctx.strokeStyle = INK;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(hx, hy, pathSize * 0.75, 0, Math.PI * 2);
      ctx.stroke();
    }
  }
}
