import { loadDesc, type Base, type Viewpoint } from "./data.ts";
import { chain, type Drainage } from "./tree.ts";
import type { Palette } from "./rivers.ts";

export interface PanelEvents {
  onFocus(i: number): void;
  onPin(i: number | null): void;
}

type Child = Node | string | null | undefined | false;

function el<K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, ...children: Child[]): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    e.append(c);
  }
  return e;
}

const fmt = new Intl.NumberFormat("en");

export class Panels {
  private base: Base;
  private events: PanelEvents;
  private search: HTMLInputElement;
  private results: HTMLUListElement;
  private focusCard: HTMLElement;
  private pathBox: HTMLElement;
  private basinBox: HTMLElement;
  private viewBox: HTMLElement;
  private d: Drainage | null = null;
  private pal: Palette | null = null;
  private descToken = 0;

  constructor(root: HTMLElement, base: Base, events: PanelEvents) {
    this.base = base;
    this.events = events;
    this.search = el("input", "search");
    this.search.type = "search";
    this.search.placeholder = "Search emoji by name or keyword";
    this.search.setAttribute("aria-label", "Search emoji");
    this.results = el("ul", "results");
    this.focusCard = el("section", "card focus");
    this.pathBox = el("section", "card path");
    this.basinBox = el("section", "card basins");
    this.viewBox = el("section", "card views");
    root.append(
      el("header", "brand", el("h1", "", "nutmap"), el("p", "muted", "Every emoji races to the focus through small semantic steps.")),
      el("div", "searchbox", this.search, this.results),
      this.focusCard,
      this.pathBox,
      this.basinBox,
      this.viewBox,
    );
    this.search.addEventListener("input", () => this.runSearch());
    this.search.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        const first = this.results.querySelector("button");
        first?.click();
      } else if (e.key === "Escape") {
        this.search.value = "";
        this.runSearch();
      }
    });
  }

  sprite(i: number, size: number): HTMLSpanElement {
    const { file, cols, rows } = this.base.meta.sprite;
    const s = el("span", "sprite");
    s.style.width = `${size}px`;
    s.style.height = `${size}px`;
    s.style.backgroundImage = `url(${file})`;
    s.style.backgroundSize = `${cols * size}px ${rows * size}px`;
    s.style.backgroundPosition = `-${(i % cols) * size}px -${Math.floor(i / cols) * size}px`;
    s.setAttribute("role", "img");
    s.setAttribute("aria-label", this.base.nodes[i].n);
    return s;
  }

  private emojiButton(i: number, size: number, label: boolean, onClick: () => void): HTMLButtonElement {
    const b = el("button", "emoji", this.sprite(i, size), label ? el("span", "", this.base.nodes[i].n) : null);
    b.type = "button";
    b.title = `${this.base.nodes[i].c} ${this.base.nodes[i].n}`;
    b.addEventListener("click", onClick);
    return b;
  }

  private runSearch(): void {
    const q = this.search.value.trim().toLowerCase();
    this.results.replaceChildren();
    if (!q) return;
    const scored: [number, number][] = [];
    this.base.nodes.forEach((node, i) => {
      const name = node.n.toLowerCase();
      let score = -1;
      if (node.c === this.search.value.trim()) score = 0;
      else if (name.startsWith(q)) score = 1;
      else if (name.includes(q)) score = 2;
      else if (node.k.some((k) => k.toLowerCase().includes(q))) score = 3;
      if (score >= 0) scored.push([score, i]);
    });
    scored.sort((a, b) => a[0] - b[0] || this.base.nodes[a[1]].n.length - this.base.nodes[b[1]].n.length);
    for (const [, i] of scored.slice(0, 20)) {
      this.results.append(
        el(
          "li",
          "",
          this.emojiButton(i, 24, true, () => {
            this.search.value = "";
            this.runSearch();
            this.events.onFocus(i);
          }),
        ),
      );
    }
    if (!scored.length) this.results.append(el("li", "muted", "No emoji match."));
  }

  setFocus(d: Drainage, pal: Palette): void {
    this.d = d;
    this.pal = pal;
    const node = this.base.nodes[d.root];
    this.focusCard.replaceChildren(
      el("p", "eyebrow", "Focus"),
      el(
        "div",
        "focusrow",
        this.sprite(d.root, 64),
        el("div", "", el("h2", "", node.n), el("p", "muted", `${node.g} · ${node.s}`)),
      ),
      el(
        "p",
        "",
        `${fmt.format(d.parent.length - 1)} emoji race here through ${d.heads.length} basins; the longest race takes ${d.maxHops} hops.`,
      ),
    );
    this.renderBasins();
    this.renderViews(d.root);
  }

  setPath(i: number | null, pinned: boolean): void {
    const d = this.d;
    if (!d) return;
    const focus = this.base.nodes[d.root];
    if (i === null) {
      this.pathBox.replaceChildren(
        el("p", "eyebrow", "Race path"),
        el("p", "muted", `Hover an emoji to see its race to ${focus.c} ${focus.n}. Click to make it the focus, Shift+click (or tap) to pin its path.`),
      );
      return;
    }
    if (i === d.root) {
      this.pathBox.replaceChildren(el("p", "eyebrow", "Race path"), el("p", "muted", "This is the focus."));
      return;
    }
    const steps = chain(d, i);
    const list = el("ol", "steps");
    for (const s of steps) {
      const rel = s.rel === 0xffff ? "" : this.base.relations[s.rel];
      list.append(
        el(
          "li",
          "",
          el(
            "div",
            "steprow",
            this.emojiButton(s.from, 24, false, () => this.events.onFocus(s.from)),
            el("span", "arrow", "→"),
            this.emojiButton(s.to, 24, false, () => this.events.onFocus(s.to)),
            el("span", "stepname", `${this.base.nodes[s.from].n} → ${this.base.nodes[s.to].n}`),
          ),
          rel ? el("p", "relation", rel) : null,
        ),
      );
    }
    const from = this.base.nodes[i];
    let unpin: HTMLButtonElement | null = null;
    if (pinned) {
      unpin = el("button", "link", "unpin");
      unpin.type = "button";
      unpin.addEventListener("click", () => this.events.onPin(null));
    }
    this.pathBox.replaceChildren(
      el("p", "eyebrow", pinned ? "Race path · pinned" : "Race path"),
      el("h3", "", `${from.c} ${from.n} → ${focus.c} ${focus.n} · ${steps.length} ${steps.length === 1 ? "hop" : "hops"} `, unpin),
      list,
    );
  }

  private renderBasins(): void {
    const d = this.d;
    const pal = this.pal;
    if (!d || !pal) return;
    const list = el("ul", "basinlist");
    for (const h of d.heads) {
      const swatch = el("span", "swatch");
      swatch.style.background = pal.headColor(h);
      const row = el("li", "", swatch, this.emojiButton(h, 24, true, () => this.events.onFocus(h)), el("span", "count", fmt.format(d.flow[h])));
      list.append(row);
    }
    this.basinBox.replaceChildren(
      el("p", "eyebrow", "Basins"),
      el("p", "muted", "Each basin is the last step before the focus; counts are the emoji that drain through it."),
      list,
    );
  }

  private async renderViews(i: number): Promise<void> {
    const token = ++this.descToken;
    this.viewBox.replaceChildren(el("p", "eyebrow", "24 viewpoints"), el("p", "muted", "Loading…"));
    let views: Viewpoint[];
    try {
      views = await loadDesc(this.base.nodes[i].id);
    } catch (err) {
      if (token === this.descToken) this.viewBox.replaceChildren(el("p", "eyebrow", "24 viewpoints"), el("p", "error", String(err)));
      return;
    }
    if (token !== this.descToken) return;
    const titles = new Map(this.base.meta.angles.map((a) => [a.id, a.title]));
    const items = views.map((v, k) => {
      const links = el("ul", "links");
      for (const l of v.l) {
        const target = l.id === null ? undefined : this.base.index.get(l.id);
        const head = target === undefined ? el("span", "rawemoji", l.e) : this.emojiButton(target, 20, false, () => this.events.onFocus(target));
        links.append(el("li", "", head, el("span", "", `: ${l.r}`)));
      }
      const details = el(
        "details",
        "",
        el("summary", "", titles.get(v.a) ?? v.a),
        el("p", "", v.d),
        v.k.length ? el("p", "muted", v.k.join(", ")) : null,
        links,
      );
      details.open = k === 0;
      return details;
    });
    this.viewBox.replaceChildren(el("p", "eyebrow", `${views.length} viewpoints`), ...items);
  }
}
