import { loadSweep, type Base } from "./data.ts";

export const BLENDED = "blended";

const FULL_SWEEP_MS = 2400;
const SHORTEST_MS = 350;
const STEEPNESS = 8;
const FADED_RIVERS = 0.25;

function logistic(x: number): number {
  return 1 / (1 + Math.exp(-x));
}

function ease(u: number): number {
  const lo = logistic(-STEEPNESS / 2);
  const hi = logistic(STEEPNESS / 2);
  return (logistic(STEEPNESS * (u - 0.5)) - lo) / (hi - lo);
}

export type Show = (xs: Float64Array, ys: Float64Array, rivers: number) => void;

interface Place {
  sweep: string;
  alpha: number;
}

interface Leg {
  sweep: string;
  from: number;
  to: number;
}

export class ViewMotion {
  private base: Base;
  private show: Show;
  private place: Place;
  private token = 0;
  private xs: Float64Array;
  private ys: Float64Array;

  constructor(base: Base, show: Show) {
    this.base = base;
    this.show = show;
    this.place = { sweep: base.meta.views.race, alpha: 1 };
    this.xs = new Float64Array(base.nodes.length);
    this.ys = new Float64Array(base.nodes.length);
  }

  async go(view: string, instant: boolean): Promise<boolean> {
    const token = ++this.token;
    const legs = this.legs(view);
    if (!legs.length) return true;
    const data = new Map<string, Float32Array>();
    for (const leg of legs) data.set(leg.sweep, await loadSweep(leg.sweep, this.base.meta));
    if (token !== this.token) return false;
    for (const leg of legs) {
      const sweep = data.get(leg.sweep);
      if (!sweep) return false;
      if (instant) this.at(sweep, leg.sweep, leg.to);
      else if (!(await this.run(token, sweep, leg))) return false;
    }
    return true;
  }

  private legs(view: string): Leg[] {
    const { sweep, alpha } = this.place;
    const goal = view === BLENDED ? null : view;
    if (goal === sweep) return alpha === 1 ? [] : [{ sweep, from: alpha, to: 1 }];
    const legs: Leg[] = [];
    if (alpha > 0) legs.push({ sweep, from: alpha, to: 0 });
    if (goal !== null) legs.push({ sweep: goal, from: 0, to: 1 });
    return legs;
  }

  private run(token: number, sweep: Float32Array, leg: Leg): Promise<boolean> {
    const duration = Math.max(SHORTEST_MS, FULL_SWEEP_MS * Math.abs(leg.to - leg.from));
    return new Promise((resolve) => {
      const start = performance.now();
      const frame = (now: number) => {
        if (token !== this.token) return resolve(false);
        const u = Math.min(1, (now - start) / duration);
        this.at(sweep, leg.sweep, leg.from + (leg.to - leg.from) * ease(u));
        if (u < 1) requestAnimationFrame(frame);
        else resolve(true);
      };
      requestAnimationFrame(frame);
    });
  }

  private at(sweep: Float32Array, name: string, alpha: number): void {
    this.place = { sweep: name, alpha };
    const race = name === this.base.meta.views.race;
    if (race && alpha === 1) {
      this.base.nodes.forEach((node, i) => {
        this.xs[i] = node.x;
        this.ys[i] = node.y;
      });
    } else {
      const alphas = this.base.meta.views.alphas;
      let k = 0;
      while (k < alphas.length - 2 && alphas[k + 1] < alpha) k++;
      const f = Math.min(1, Math.max(0, (alpha - alphas[k]) / (alphas[k + 1] - alphas[k])));
      const stride = this.xs.length * 2;
      const a = k * stride;
      const b = a + stride;
      for (let i = 0; i < this.xs.length; i++) {
        this.xs[i] = sweep[a + 2 * i] + (sweep[b + 2 * i] - sweep[a + 2 * i]) * f;
        this.ys[i] = sweep[a + 2 * i + 1] + (sweep[b + 2 * i + 1] - sweep[a + 2 * i + 1]) * f;
      }
    }
    this.show(this.xs, this.ys, race ? FADED_RIVERS + (1 - FADED_RIVERS) * alpha : FADED_RIVERS);
  }
}
