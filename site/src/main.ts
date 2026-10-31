import "./style.css";
import { loadBase, loadTree, type Base } from "./data.ts";
import { drainage, type Drainage } from "./tree.ts";
import { MapView } from "./view.ts";
import { Panels } from "./panels.ts";

function shell(app: HTMLElement): { canvas: HTMLCanvasElement; panel: HTMLElement; status: HTMLElement } {
  const canvas = document.createElement("canvas");
  canvas.setAttribute("aria-label", "Emoji drainage map");
  const status = document.createElement("div");
  status.className = "status";
  const hint = document.createElement("div");
  hint.className = "hint";
  hint.textContent = "Scroll or pinch to zoom · drag to pan · click an emoji to focus it";
  const map = document.createElement("div");
  map.className = "map";
  map.append(canvas, hint, status);
  const panel = document.createElement("aside");
  panel.className = "panel";
  app.replaceChildren(map, panel);
  return { canvas, panel, status };
}

function start(base: Base, canvas: HTMLCanvasElement, panel: HTMLElement, status: HTMLElement): void {
  let focusId: string | null = null;
  let pinned: number | null = null;
  let hovered: number | null = null;
  let token = 0;
  let current: Drainage | null = null;

  const parse = (): [string, string | null] => {
    const [f, s] = decodeURIComponent(location.hash.slice(1)).split("/");
    return [base.index.has(f) ? f : base.meta.root, s && base.index.has(s) ? s : null];
  };

  const hash = (f: string, s: string | null) => (s ? `#${f}/${s}` : `#${f}`);

  const focusTo = (i: number) => {
    location.hash = hash(base.nodes[i].id, null);
  };

  const pinTo = (i: number | null) => {
    if (!focusId) return;
    history.replaceState(null, "", hash(focusId, i === null ? null : base.nodes[i].id));
    void apply();
  };

  const view = new MapView(canvas, base, {
    onFocus: focusTo,
    onPin: pinTo,
    onHover: (i) => {
      hovered = i;
      panels.setPath(hovered ?? pinned, hovered === null && pinned !== null);
    },
  });
  const panels = new Panels(panel, base, { onFocus: focusTo, onPin: pinTo });

  async function apply(): Promise<void> {
    const [f, s] = parse();
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
  const { canvas, panel, status } = shell(app);
  status.textContent = "Loading emoji…";
  try {
    const base = await loadBase();
    status.textContent = "";
    start(base, canvas, panel, status);
  } catch (err) {
    status.textContent = `Could not load the map data: ${String(err)}`;
  }
}

void main();
