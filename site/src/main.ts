import "./style.css";
import { loadBase, loadTree, type Base } from "./data.ts";
import { drainage, type Drainage } from "./tree.ts";
import { MapView } from "./view.ts";
import { Panels } from "./panels.ts";
import { BLENDED, ViewMotion } from "./motion.ts";

interface Shell {
  canvas: HTMLCanvasElement;
  panel: HTMLElement;
  status: HTMLElement;
  caption: HTMLElement;
}

function shell(app: HTMLElement): Shell {
  const canvas = document.createElement("canvas");
  canvas.setAttribute("aria-label", "Emoji drainage map");
  const status = document.createElement("div");
  status.className = "status";
  const caption = document.createElement("div");
  caption.className = "caption";
  const hint = document.createElement("div");
  hint.className = "hint";
  hint.textContent = "Scroll or pinch to zoom · drag to pan · click an emoji to focus it";
  const map = document.createElement("div");
  map.className = "map";
  map.append(canvas, caption, hint, status);
  const panel = document.createElement("aside");
  panel.className = "panel";
  app.replaceChildren(map, panel);
  return { canvas, panel, status, caption };
}

function start(base: Base, { canvas, panel, status, caption }: Shell): void {
  const race = base.meta.views.race;
  const views = new Set([race, BLENDED, ...base.meta.views.sweeps.map((s) => s.id)]);
  let focusId: string | null = null;
  let pinnedId: string | null = null;
  let pinned: number | null = null;
  let hovered: number | null = null;
  let shown: string | null = null;
  let token = 0;
  let current: Drainage | null = null;

  const parse = (): [string, string | null, string] => {
    const [f, s, v] = decodeURIComponent(location.hash.slice(1)).split("/");
    return [base.index.has(f) ? f : base.meta.root, s && base.index.has(s) ? s : null, v && views.has(v) ? v : race];
  };

  const hash = (f: string, s: string | null, v: string | null) => {
    const map = v ?? race;
    if (map !== race) return `#${f}/${s ?? ""}/${map}`;
    return s ? `#${f}/${s}` : `#${f}`;
  };

  const focusTo = (i: number) => {
    location.hash = hash(base.nodes[i].id, null, shown);
  };

  const pinTo = (i: number | null) => {
    if (!focusId) return;
    history.replaceState(null, "", hash(focusId, i === null ? null : base.nodes[i].id, shown));
    void apply();
  };

  const viewTo = (v: string) => {
    location.hash = hash(focusId ?? base.meta.root, pinnedId, v);
  };

  const view = new MapView(canvas, base, {
    onFocus: focusTo,
    onPin: pinTo,
    onHover: (i) => {
      hovered = i;
      panels.setPath(hovered ?? pinned, hovered === null && pinned !== null);
    },
  });
  const panels = new Panels(panel, base, { onFocus: focusTo, onPin: pinTo, onView: viewTo });
  const motion = new ViewMotion(base, (xs, ys, rivers) => view.setPositions(xs, ys, rivers));

  async function moveTo(v: string): Promise<void> {
    const first = shown === null;
    shown = v;
    panels.setView(v);
    caption.textContent = v === race ? "" : panels.title(v);
    try {
      await motion.go(v, first);
    } catch (err) {
      status.textContent = `Could not load that map: ${String(err)}`;
    }
  }

  async function apply(): Promise<void> {
    const [f, s, v] = parse();
    if (v !== shown) void moveTo(v);
    if (f !== focusId) {
      focusId = f;
      const t = ++token;
      status.textContent = "Loading race tree…";
      try {
        const tree = await loadTree(f, base.meta.n);
        if (t !== token) return;
        current = drainage(tree);
      } catch (err) {
        status.textContent = `Could not load the race tree: ${String(err)}`;
        return;
      }
      status.textContent = "";
      view.setDrainage(current);
      const pal = view.palette;
      if (pal) panels.setFocus(current, pal);
      const node = base.nodes[current.root];
      document.title = `${node.c} ${node.n} · nutmap`;
    }
    pinnedId = s;
    pinned = s === null ? null : (base.index.get(s) ?? null);
    view.setPinned(pinned);
    panels.setPath(hovered ?? pinned, hovered === null && pinned !== null);
  }

  window.addEventListener("hashchange", () => void apply());
  void apply();
}

async function main(): Promise<void> {
  const app = document.querySelector<HTMLElement>("#app");
  if (!app) throw new Error("#app missing");
  const parts = shell(app);
  const { status } = parts;
  status.textContent = "Loading emoji…";
  try {
    const base = await loadBase();
    status.textContent = "";
    start(base, parts);
  } catch (err) {
    status.textContent = `Could not load the map data: ${String(err)}`;
  }
}

void main();
