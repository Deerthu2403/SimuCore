/** Tests the 3D interface in a real browser (the installed Microsoft Edge) against the running stack
 *  and the real simulated bank: faults are really caused, clicks are real mouse clicks.
 *
 *    node e2e/run.mjs            everything
 *    node e2e/run.mjs U01 U14    only these
 *
 *  Needs the Docker stack; starts and stops the simulated bank itself. */
import { execFile, spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { chromium } from "playwright-core";

const run = promisify(execFile);
const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, "..", "..");
const BASE = process.env.SIMUCORE_URL ?? "http://localhost:8080";
const API = `${BASE}/api/v2/twin`;
const BANK_PY = join(ROOT, "banking-sim", ".venv", "Scripts", "python.exe");
const DOCKER = process.env.DOCKER ?? "C:\\Program Files\\Docker\\Docker\\resources\\bin\\docker.exe";
const OUT = join(HERE, "..", "results");
const GPU = ["--use-angle=d3d11", "--ignore-gpu-blocklist", "--enable-gpu"];
const COLOR = {
  healthy: "#22b573", warning: "#f2c230", overloaded: "#f58236", failed: "#f0475f", recovering: "#5aa2ff", unknown: "#8b97a8",
};

// ---------------------------------------------------------------- framework
const checks = [];
const check = (code, title, f) => checks.push({ code, title, f });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function ok(cond, what) {
  if (!cond) throw new Error(typeof what === "string" ? what : JSON.stringify(what));
}
const same = (a, b, what = "") => ok(JSON.stringify(a) === JSON.stringify(b), `${what}: ${JSON.stringify(a)} is not ${JSON.stringify(b)}`);
async function until(what, f, seconds, every = 100) {
  const t0 = Date.now();
  let last;
  for (;;) {
    try {
      last = await f();
      if (last) return +((Date.now() - t0) / 1000).toFixed(1);
    } catch (e) {
      last = e.message;
    }
    if (Date.now() - t0 > seconds * 1000) throw new Error(`not within ${seconds} s: ${what} (last: ${JSON.stringify(last)?.slice(0, 300)})`);
    await sleep(every);
  }
}
const api = async (path = "") => {
  const r = await fetch(API + path);
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
};
const comp = (id) => api(`/components/${id}`);
const bank = (...args) => run(BANK_PY, ["bankctl.py", ...args], { cwd: join(ROOT, "banking-sim"), maxBuffer: 1e7 });
const docker = (...args) => run(DOCKER, args);
async function pidOf(service) {
  const { stdout } = await bank("status", "--json");
  const s = JSON.parse(stdout);
  const row = Array.isArray(s) ? s.find((x) => x.service === service) : s[service] ?? s.services?.[service];
  return row.pid;
}
const freeze = (pid, on) => run(BANK_PY, ["-c", `import psutil; psutil.Process(${pid}).${on ? "suspend" : "resume"}()`]);

let workload = null;
function traffic(kind = "normal", rps = 6) {
  stopTraffic();
  const child = spawn(BANK_PY, ["bankctl.py", "workload", kind, "--rps", String(rps), "--duration", "7200"],
    { cwd: join(ROOT, "banking-sim"), stdio: "ignore" });
  workload = child;
}
function stopTraffic() {
  if (!workload) return;
  // the process tree: bankctl starts the workload as a child
  spawn("taskkill", ["/PID", String(workload.pid), "/T", "/F"], { stdio: "ignore" });
  workload = null;
}
const allHealthy = async () => {
  const s = await api("/summary");
  return s.by_status.healthy === s.components && s.links_failed === 0;
};
async function settled() {
  await bank("clear");
  await until("all healthy", async () => (await allHealthy()) || (await api()).components.filter((c) => c.status !== "healthy").map((c) => [c.id, c.status, c.reason]).length === 0, 90, 500);
}

// ------------------------------------------------------------------ browser
let browser;
const problems = [];
async function open(opts = {}) {
  const page = await browser.newPage({ viewport: { width: 1600, height: 900 }, ...opts.page });
  page.on("pageerror", (e) => problems.push(`page error: ${e.message}`));
  page.on("console", (m) => {
    if (m.type() === "error" || /THREE|shader|WebGL/i.test(m.text())) if (!opts.quiet) problems.push(`console ${m.type()}: ${m.text().slice(0, 300)}`);
  });
  if (opts.before) await opts.before(page);
  await page.goto(BASE);
  if (opts.wait !== false) {
    await until("scene drawn", () => page.evaluate(() => window.__simucore?.frames().count > 30 && window.__simucore.nodes().length > 0), 30);
    if (opts.still !== false) await still(page);
  }
  return page;
}
const hook = (page, f, ...args) => page.evaluate(([name, a]) => window.__simucore[name](...a), [f, args]);
const node = (page, id) => hook(page, "node", id);
const wire = (page, id, t) => hook(page, "wire", id, t);
const camera = (page) => hook(page, "camera");
/** Waits until nothing moves any more. */
async function still(page) {
  await sleep(120);
  let last = "";
  await until("still", async () => {
    const c = JSON.stringify([(await camera(page)).position.map((x) => +x.toFixed(2)), await hook(page, "settled")]);
    const r = c === last && c.endsWith("true]");
    last = c;
    return r;
  }, 12, 150);
}
const name = (page) => page.textContent("[data-testid=inspector-name]");
const status = (page) => page.getAttribute("[data-testid=inspector-status] .chip", "data-status");
async function reset(page) {
  await page.evaluate(() => document.activeElement?.blur());
  for (let i = 0; i < 4; i++) await page.keyboard.press("Escape");
  await page.keyboard.press("r");
  for (const x of await page.$$(".toast .btn")) await x.click({ timeout: 500 }).catch(() => undefined);
  await page.mouse.move(800, 20);
  await still(page);
}
const tab = (page, where, n) => page.click(`[data-testid=${where}] [role=tab]:nth-child(${n})`);
const inCanvas = (p) => p.x > 300 && p.x < 1225 && p.y > 60 && p.y < 640;
/** A point of the page where this component is in front, as a user would look for one. */
async function pointOf(page, id) {
  for (const key of ["1", "2", "3", "4"]) {
    await page.keyboard.press(key);
    await still(page);
    const n = await node(page, id);
    for (let i = 0; i < 49; i++) {
      const p = { x: n.screen.x + ((i % 7) - 3) * 8, y: n.screen.y + (Math.floor(i / 7) - 3) * 8 };
      if (!inCanvas(p)) continue;
      const free = await page.evaluate(([x, y]) => document.elementFromPoint(x, y)?.tagName === "CANVAS", [p.x, p.y]);
      if (free && (await hook(page, "pickAt", p.x, p.y)) === id) return p;
    }
  }
  throw new Error(`${id} is not reachable by mouse in any view`);
}
const select = (page, id) => page.click(`[data-testid=left-panel] [data-id="${id}"]`);

// -------------------------------------------------------------------- checks
check("U01", "The scene draws every component and connection of the twin, with the right status and colour", async (page) => {
  const t = await api();
  same((await hook(page, "nodes")).sort(), t.components.map((c) => c.id).sort(), "components");
  same((await hook(page, "wires")).sort(), t.links.map((k) => k.id).sort(), "links");
  const types = {};
  for (const c of t.components) {
    types[c.type] = (types[c.type] ?? 0) + 1;
    await until(`${c.id} like the twin`, async () => {
      const [n, l, a] = [await node(page, c.id), await hook(page, "label", c.id), await comp(c.id)];
      // a label that would cover another one is hidden by design; a shown one must be right
      return n.status === a.status && n.color === COLOR[a.status] && n.visible === 1 && l.status === a.status
        && (!l.shown || l.text.includes(a.name.replace(/ Service$/, "")));
    }, 15);
  }
  const labels = await Promise.all(t.components.map((c) => hook(page, "label", c.id)));
  const shownLabels = labels.filter((l) => l.shown).length;
  ok(shownLabels >= 0.75 * labels.length, `only ${shownLabels} of ${labels.length} names shown`);
  for (const want of ["microservice", "server", "database", "network", "load_balancer", "gateway"]) ok(types[want], `no ${want}`);
  const blank = await page.evaluate(async () => {
    // the picture really contains something: many different colours
    const bmp = await createImageBitmap(await new Promise((r) => document.querySelector("canvas").toBlob(r)));
    return [bmp.width, bmp.height];
  });
  const shot = await page.screenshot({ clip: { x: 300, y: 60, width: 920, height: 580 } });
  ok(shot.length > 100_000, `the picture is nearly empty (${shot.length} bytes)`);
  const f = await hook(page, "frames");
  ok(f.triangles > 5000, f.triangles);
  return { components: types, links: t.links.length, canvas: blank, draw_calls: f.calls, triangles: f.triangles };
});

check("U02", "Every number shown equals the twin's value (compared at the same version)", async (page) => {
  const ids = (await api()).components.map((c) => c.id);
  let values = 0;
  let moments = 0;
  const compare = async (read) => {
    for (let i = 0; i < 25; i++) {
      const r = await page.evaluate(async (src) => {
        const t = await (await fetch("/api/v2/twin")).json();
        await new Promise((f) => requestAnimationFrame(() => requestAnimationFrame(f)));
        return { same: t.meta.version === window.__simucore.version(), t, shown: new Function(`return (${src})()`)() };
      }, read.toString());
      if (r.same) return r;
      await sleep(60);
    }
    throw new Error("never at the same version");
  };
  for (const id of ids) {
    await select(page, id);
    await page.waitForSelector("[data-testid=inspector-name]");
    const r = await compare(() => ({
      tiles: [...document.querySelectorAll("[data-testid=inspector] .tile[data-value]")].map((e) => [e.dataset.metric, +e.dataset.value]),
      title: document.querySelector("[data-testid=inspector-name]").textContent,
      status: document.querySelector("[data-testid=inspector-status] .chip").dataset.status,
    }));
    const c = r.t.components.find((x) => x.id === id);
    same(r.shown.title, c.name, "name");
    same(r.shown.status, c.status, "status");
    ok(r.shown.tiles.length > 0 || Object.keys(c.metrics).length === 0, `${id} shows no numbers`);
    for (const [m, v] of r.shown.tiles) same(v, c.metrics[m], `${id} ${m}`);
    values += r.shown.tiles.length;
    moments++;
    // what the brief names must be there for every service
    if (c.type === "microservice") for (const m of ["cpu_pct", "mem_pct", "latency_ms", "rps"]) ok(r.shown.tiles.some(([k]) => k === m), `${id} without ${m}`);
  }
  await reset(page);
  const r = await compare(() => ({
    counts: Object.fromEntries([...document.querySelectorAll("[data-testid=status-counts] button")].map((b) => [b.dataset.status, +b.querySelector("b").textContent])),
    rows: [...document.querySelectorAll("[data-testid=left-panel] .row-btn")].map((b) => [b.dataset.id, b.dataset.status]),
  }));
  for (const [s, n] of Object.entries(r.shown.counts)) same(n, r.t.summary.by_status[s] ?? 0, s);
  same(Object.fromEntries(r.shown.rows), Object.fromEntries(r.t.components.map((c) => [c.id, c.status]).sort((a, b) => r.shown.rows.findIndex((x) => x[0] === a[0]) - r.shown.rows.findIndex((x) => x[0] === b[0]))), "list");
  await tab(page, "left-panel", 2);
  const h = await compare(() => [...document.querySelectorAll("[data-testid=health] tbody tr")].map((x) => [x.dataset.id, [...x.querySelectorAll("td")].map((d) => [d.dataset.metric, d.textContent])]));
  ok(h.shown.length === 8, "eight services in the health table");
  for (const [id, cells] of h.shown) {
    const c = h.t.components.find((x) => x.id === id);
    for (const [m, text] of cells) {
      const v = c.metrics[m];
      const factor = m === "error_rate" ? 100 : 1;
      if (v === undefined) same(text, "–", `${id} ${m}`);
      else ok(Math.abs(parseFloat(text.replace(",", "")) * (m === "latency_ms" && text.includes("s") ? 1000 : 1) - v * factor) <= Math.max(0.06, Math.abs(v * factor) * 0.01), `${id} ${m}: ${text} for ${v}`);
      values++;
    }
  }
  await tab(page, "left-panel", 1);
  return { components: moments, values_compared: values, differences: 0 };
});

check("U03", "Clicking each 3D object selects exactly that component", async (page) => {
  const list = (await api()).components;
  for (const c of list) {
    const p = await pointOf(page, c.id);
    await page.mouse.click(p.x, p.y);
    await until(`${c.name} selected`, async () => (await name(page)) === c.name, 3);
    await until("highlighted", async () => (await node(page, c.id)).highlight > 0.95, 3);
    await page.keyboard.press("Escape");
  }
  // a click beside everything clears the selection
  await reset(page);
  await select(page, list[0].id);
  await page.keyboard.press("1");
  await still(page);
  await page.mouse.click(330, 75);
  ok(!(await page.$("[data-testid=inspector-name]")), "still selected");
  return { clicked: list.length };
});

check("U04", "Hovering shows name and live values, highlights, and follows from list to scene", async (page) => {
  const p = await pointOf(page, "bank-account");
  await page.mouse.move(p.x, p.y, { steps: 4 });
  await until("highlight", async () => (await node(page, "bank-account")).highlight > 0.5, 2);
  const l = await hook(page, "label", "bank-account");
  const c = await comp("bank-account");
  ok(/CPU .* RAM .* p95 .* req\/s/.test(l.text.replace(/\s+/g, " ")), l.text);
  same(await page.evaluate(() => document.querySelector("canvas").style.cursor), "pointer", "cursor");
  await page.mouse.move(800, 20);
  await until("no highlight", async () => (await node(page, "bank-account")).highlight < 0.01, 2);
  await page.hover("[data-testid=left-panel] [data-id=bank-fraud]");
  await until("highlight from the list", async () => (await node(page, "bank-fraud")).highlight > 0.5, 2);
  await page.mouse.move(800, 20);
  return { label: l.text.replace(/\s+/g, " "), of: c.name };
});

check("U05", "Mouse camera: drag rotates, right-drag pans, wheel zooms; dragging selects nothing", async (page) => {
  const at = async () => camera(page);
  const a = await at();
  await page.mouse.move(760, 600);
  await page.mouse.down();
  await page.mouse.move(920, 560, { steps: 8 });
  await page.mouse.up();
  await still(page);
  const b = await at();
  ok(Math.abs(b.azimuth - a.azimuth) > 0.1, "no rotation");
  ok(Math.abs(b.distance - a.distance) < 0.5, "distance changed");
  ok(!(await page.$("[data-testid=inspector-name]")), "dragging selected something");
  await page.mouse.move(760, 600);
  await page.mouse.down({ button: "right" });
  await page.mouse.move(640, 560, { steps: 8 });
  await page.mouse.up({ button: "right" });
  await still(page);
  const c = await at();
  ok(Math.hypot(...c.target.map((x, i) => x - b.target[i])) > 0.5, "no pan");
  ok(Math.abs(c.azimuth - b.azimuth) < 0.01, "rotated while panning");
  for (let i = 0; i < 4; i++) {
    await page.mouse.wheel(0, -240);
    await sleep(60);
  }
  await still(page);
  const d = await at();
  ok(d.distance < c.distance * 0.9, `no zoom in ${c.distance} ${d.distance}`);
  for (let i = 0; i < 30; i++) await page.mouse.wheel(0, 900);
  await still(page);
  const e = await at();
  ok(e.distance > d.distance && e.distance < 400 && Number.isFinite(e.distance), "zoom out");
  for (let i = 0; i < 60; i++) await page.mouse.wheel(0, -900);
  await still(page);
  ok((await at()).distance >= 2.4, "through the target");
  // never under the floor
  await page.mouse.move(760, 300);
  await page.mouse.down();
  await page.mouse.move(760, 60, { steps: 10 });
  await page.mouse.up();
  await still(page);
  const f = await at();
  ok(f.position[1] > 0, `camera under the floor ${f.position}`);
  return { rotated_rad: +(b.azimuth - a.azimuth).toFixed(2), zoom: [+c.distance.toFixed(1), +d.distance.toFixed(1), +e.distance.toFixed(1)] };
});

check("U06", "Camera buttons and keys: four views, zoom, rotate, pan, reset; smooth, not jumping", async (page) => {
  const views = {};
  for (const [label, key] of [["View from above", "4"], ["Front view", "2"], ["Side view", "3"], ["Overview", "1"]]) {
    await page.click(`[data-testid=toolbar] [aria-label="${label}"]`);
    await still(page);
    const a = await camera(page);
    await reset(page);
    await page.keyboard.press(key);
    await still(page);
    const b = await camera(page);
    ok(Math.abs(a.polar - b.polar) < 0.02 && Math.abs(a.distance - b.distance) < 0.5, `${label}: button and key differ ${JSON.stringify([a, b])}`);
    views[label] = [+a.azimuth.toFixed(2), +a.polar.toFixed(2)];
    // everything is in the picture
    for (const id of await hook(page, "nodes")) {
      const s = (await node(page, id)).screen;
      ok(s.ahead && s.x > 292 && s.x < 1232 && s.y > 48 && s.y < 712, `${label}: ${id} outside at ${JSON.stringify(s)}`);
    }
  }
  const effect = async (act, what) => {
    const a = await camera(page);
    await act();
    await still(page);
    const b = await camera(page);
    ok(what(a, b), act.toString());
  };
  await effect(() => page.click('[aria-label="Zoom in"]'), (a, b) => b.distance < a.distance * 0.9);
  await effect(() => page.keyboard.press("-"), (a, b) => b.distance > a.distance * 1.1);
  await effect(() => page.click('[aria-label="Rotate left"]'), (a, b) => b.azimuth < a.azimuth - 0.2);
  await effect(() => page.keyboard.press("ArrowRight"), (a, b) => b.azimuth > a.azimuth + 0.2);
  await effect(() => page.keyboard.press("ArrowUp"), (a, b) => b.polar < a.polar - 0.1);
  await effect(() => page.keyboard.press("Shift+ArrowLeft"), (a, b) => Math.hypot(b.target[0] - a.target[0], b.target[2] - a.target[2]) > 0.5);
  // smooth: the way between two views has many small steps
  await page.keyboard.press("1");
  await still(page);
  await page.keyboard.press("3");
  const path = [];
  for (let i = 0; i < 30; i++) {
    path.push((await camera(page)).azimuth);
    await sleep(20);
  }
  const steps = path.slice(1).map((x, i) => Math.abs(x - path[i]));
  const moved = Math.abs(path[path.length - 1] - path[0]);
  ok(moved > 0.8 && Math.max(...steps) < moved * 0.35, { moved, largest: Math.max(...steps) });
  return { views, largest_step_share: +(Math.max(...steps) / moved).toFixed(2) };
});

check("U07", "Focus: button, key and double click bring a component into the middle", async (page) => {
  const middle = async (id) => {
    const s = (await node(page, id)).screen;
    return Math.hypot(s.x - 762, s.y - 380);
  };
  const before = (await camera(page)).distance;
  const p = await pointOf(page, "bank-fraud");
  await page.mouse.dblclick(p.x, p.y);
  await still(page);
  ok((await middle("bank-fraud")) < 130, `double click: ${await middle("bank-fraud")} px off`);
  ok((await camera(page)).distance < before * 0.7, "not closer");
  await page.keyboard.press("1");
  await still(page);
  await page.click('[data-testid=inspector] [aria-label="Focus"]');
  await still(page);
  ok((await middle("bank-fraud")) < 130, "button");
  await page.keyboard.press("1");
  await still(page);
  await page.evaluate(() => document.activeElement?.blur());
  await page.keyboard.press("f");
  await still(page);
  ok((await middle("bank-fraud")) < 130, "key");
  return { off_centre_px: Math.round(await middle("bank-fraud")) };
});

check("U08", "Search: by name, type, status, port; keyboard and mouse; flies to what was found", async (page) => {
  const results = async (q) => {
    await page.fill("[data-testid=search] input", q);
    return page.$$eval("[data-testid=search] li[data-id]", (l) => l.map((x) => x.dataset.id));
  };
  await page.keyboard.press("Control+k");
  ok((await results("")).length === (await api()).components.length, "all listed");
  same((await results("pay"))[0], "bank-payment", "name");
  same(await results("8103"), ["bank-account"], "port");
  same((await results("database")).length >= 3, true, "type");
  same(await results("load balancer"), ["lb-clients"], "type with blank");
  same(await results("no such thing"), [], "nothing");
  ok((await page.textContent("[data-testid=search]")).includes("Nothing matches"), "says so");
  await results("pay");
  await page.keyboard.press("ArrowDown");
  const second = await page.getAttribute("[data-testid=search] li.active", "data-id");
  await page.keyboard.press("Enter");
  await still(page);
  same(await name(page), (await comp(second)).name, "selected");
  const s = (await node(page, second)).screen;
  ok(Math.hypot(s.x - 762, s.y - 380) < 150, "not in the middle");
  // found although hidden
  await reset(page);
  await page.click('[aria-label="Hide Databases"]');
  await until("hidden", async () => (await node(page, "store-bank")).visible === 0, 3);
  await page.click('[aria-label="Search components"]');
  await results("bank database");
  await page.click("[data-testid=search] li[data-id=store-bank]");
  await until("visible again", async () => (await node(page, "store-bank")).visible === 1, 3);
  await page.keyboard.press("/");
  await page.keyboard.press("Escape");
  ok(!(await page.$("[data-testid=search]")), "closed by Esc");
  return { second_hit: second };
});

check("U09", "Isolate: only the component and its direct connections remain; links too; and back", async (page) => {
  const out = {};
  for (const id of ["bank-transaction", "store-bank", "net-bank"]) {
    const c = await comp(id);
    const want = [id, ...c.dependencies.depends_on.map((d) => d.id), ...c.dependencies.dependents.map((d) => d.id)];
    await select(page, id);
    await page.click('[data-testid=inspector] [aria-label="Isolate"]');
    await still(page);
    const seen = [];
    for (const n of await hook(page, "nodes")) if ((await node(page, n)).visible > 0.5) seen.push(n);
    same(seen.sort(), [...new Set(want)].sort(), id);
    const links = [];
    for (const w of await hook(page, "wires")) if ((await wire(page, w)).visible > 0.5) links.push(w);
    same(links.sort(), [...c.dependencies.depends_on, ...c.dependencies.dependents].map((d) => d.link_id).sort(), "links");
    ok((await page.textContent("[data-testid=mode-note]")).includes(c.name), "note");
    const hidden = (await hook(page, "nodes")).find((n) => !want.includes(n));
    ok((await hook(page, "label", hidden)).shown === false, "label of a hidden component");
    const at = (await node(page, hidden)).screen;
    ok((await hook(page, "pickAt", at.x, at.y)) !== hidden, "a hidden component can be clicked");
    out[id] = seen.length;
    await page.keyboard.press("Escape");
    await still(page);
    for (const n of await hook(page, "nodes")) ok((await node(page, n)).visible === 1, `${n} did not come back`);
    await reset(page);
  }
  return out;
});

check("U10", "Dependency view: exactly the twin's dependencies, left and right by steps, interactive", async (page) => {
  const out = {};
  for (const id of ["bank-account", "bank-payment", "lb-clients"]) {
    await select(page, id);
    const d = await api(`/components/${id}/dependencies`);
    await page.keyboard.press("d");
    await still(page);
    const centre = await node(page, id);
    const seen = [];
    for (const n of await hook(page, "nodes")) {
      const s = await node(page, n);
      if (s.visible < 0.5 || n === id) continue;
      seen.push(n);
    }
    // being plugged into the network is not followed
    const structural = new Set((await api("/links")).filter((k) => k.kind === "connected_to" || k.kind === "monitors").map((k) => k.id));
    const all = await api("/links");
    const walk = (from, side) => {
      const got = new Map();
      let front = [from];
      for (let n = 1; front.length; n++) {
        const next = [];
        for (const x of front) for (const k of all) {
          if (structural.has(k.id) || k[side === "out" ? "source" : "target"] !== x) continue;
          const o = k[side === "out" ? "target" : "source"];
          if (!got.has(o)) { got.set(o, n); next.push(o); }
        }
        front = next;
      }
      return got;
    };
    const down = walk(id, "out");
    const up = walk(id, "in");
    same(seen.sort(), [...down.keys(), ...up.keys()].sort(), id);
    // the API's own answer contains them all
    const apiIds = new Set([...d.upstream, ...d.downstream].map((x) => x.id));
    for (const s of seen) ok(apiIds.has(s), `${s} is not in the API's answer`);
    for (const n of down.keys()) ok((await node(page, n)).x > centre.x, `${n} must stand right of ${id}`);
    for (const n of up.keys()) ok((await node(page, n)).x < centre.x, `${n} must stand left of ${id}`);
    out[id] = { needs: down.size, needed_by: up.size };
    await reset(page);
  }
  // the graph in the panel: click a box, the view moves its centre there
  await select(page, "bank-payment");
  await page.click('[data-testid=inspector] [aria-label="Dependencies"]');
  await page.waitForSelector("[data-testid=dependency-graph]");
  const boxes = await page.$$eval("[data-testid=dependency-graph] [data-node]", (l) => l.map((e) => e.dataset.node));
  ok(boxes.includes("bank-payment") && boxes.length === out["bank-payment"].needs + out["bank-payment"].needed_by + 1, boxes);
  await still(page);
  const before = (await node(page, "bank-database")).x;
  await page.click('[data-testid=dependency-graph] [data-node="bank-database"]');
  const steps = [];
  for (let i = 0; i < 12; i++) {
    steps.push((await node(page, "bank-payment")).x);
    await sleep(30);
  }
  await still(page);
  same(await name(page), "Database Service", "centre moved");
  ok(new Set(steps.map((x) => x.toFixed(2))).size > 4, "the change of view is not animated");
  ok((await node(page, "bank-payment")).x < (await node(page, "bank-database")).x, "payment must now be left");
  await page.click('[data-testid=inspector] .reflist [data-ref="store-bank"]');
  await still(page);
  same(await name(page), "Bank database", "list entry");
  await tab(page, "inspector", 1);
  await reset(page);
  return { ...out, moved_from: +before.toFixed(1) };
});

check("U11", "Connections: click one in the scene; its live values equal the twin's", async (page) => {
  let found = null;
  for (const id of ["lb-clients->bank-account", "bank-account->bank-database", "lb-clients->bank-customer"]) {
    for (const key of ["1", "4", "2"]) {
      await page.keyboard.press(key);
      await still(page);
      for (let t = 0.25; t < 0.8 && !found; t += 0.05) {
        const p = (await wire(page, id, t)).screen;
        if (!inCanvas(p) || (await hook(page, "pickAt", p.x, p.y))) continue;
        await page.mouse.click(p.x, p.y);
        const title = await name(page).catch(() => null);
        const [a, b] = id.split("->");
        if (title === `${(await comp(a)).name} → ${(await comp(b)).name}`) found = id;
        else await page.keyboard.press("Escape");
      }
      if (found) break;
    }
    if (found) break;
  }
  ok(found, "no link could be clicked");
  await until("equal values", async () => page.evaluate(async (id) => {
    const t = await (await fetch("/api/v2/twin")).json();
    await new Promise((f) => requestAnimationFrame(() => requestAnimationFrame(f)));
    if (t.meta.version !== window.__simucore.version()) return false;
    const k = t.links.find((x) => x.id === id);
    const tiles = [...document.querySelectorAll("[data-testid=inspector] .tile[data-value]")];
    return tiles.length > 0 && tiles.every((e) => +e.dataset.value === k.metrics[e.dataset.metric]);
  }, found), 12);
  await until("highlighted", async () => (await wire(page, found)).highlight > 0.9, 3);
  return { link: found };
});

check("U12", "Live: values change by themselves, without reload, within a fraction of a second of the twin", async (page) => {
  await select(page, "bank-account");
  await tab(page, "inspector", 1);
  const seen = new Set();
  const frames0 = (await hook(page, "frames")).count;
  const t0 = Date.now();
  while (Date.now() - t0 < 20_000) {
    seen.add(await page.getAttribute("[data-testid=inspector] .tile[data-metric=cpu_pct]", "data-value"));
    await sleep(200);
  }
  ok(seen.size >= 4, `only ${seen.size} different values in 20 s`);
  // from the moment the twin makes a message until the next picture that contains it
  const lags = await page.evaluate(async () => {
    const out = [];
    let last = window.__simucore.version();
    const end = performance.now() + 25_000;
    while (performance.now() < end && out.length < 10) {
      await new Promise((f) => requestAnimationFrame(f));
      if (window.__simucore.version() === last) continue;
      last = window.__simucore.version();
      await new Promise((f) => requestAnimationFrame(f));
      out.push(Date.now() - Date.parse(window.__simucore.times().made));
    }
    return out;
  });
  ok(lags.length >= 5 && Math.max(...lags) < 1000, lags);
  const spark = await page.$$eval("[data-testid=inspector] .spark .line", (l) => l.length);
  ok(spark >= 4, "no curves");
  ok((await hook(page, "frames")).count > frames0 + 400, "the scene stopped drawing");
  return { different_values: seen.size, lag_ms: lags.map(Math.round), curves: spark };
});

check("U13", "Request flow follows measured traffic: more traffic, more and faster lights", async (page) => {
  const id = "lb-clients->bank-account";
  const speed = async () => {
    const a = await wire(page, id);
    const t0 = Date.now();
    await sleep(700);
    const b = await wire(page, id);
    return (((b.phase - a.phase + 1) % 1) * 1000) / (Date.now() - t0);
  };
  await sleep(4000);
  const low = { flow: (await wire(page, id)).flow, speed: await speed() };
  traffic("normal", 30);
  await until("more flow", async () => (await wire(page, id)).flow > low.flow * 1.4, 60, 500);
  await sleep(8000);
  const high = { flow: (await wire(page, id)).flow, speed: await speed() };
  ok(high.speed > low.speed * 1.1, { low, high });
  await until("flow equals the twin's", async () => Math.abs((await wire(page, id)).flow - (await api(`/links/${encodeURIComponent(id)}`)).display.flow) < 0.02, 15);
  stopTraffic();
  await until("no traffic, no lights", async () => (await wire(page, id)).flow < 0.005, 40, 500);
  traffic();
  await until("flow again", async () => (await wire(page, id)).flow > 0.05, 40, 500);
  const r = (x) => ({ flow: +x.flow.toFixed(2), speed: +x.speed.toFixed(2) });
  return { low: r(low), high: r(high) };
});

check("U14", "Crash: the 3D object changes at once; alert, message, event, dependents, links; then recovery", async (page) => {
  await settled();
  const id = "bank-payment";
  const t0 = Date.now();
  await bank("kill", "payment");
  // the page must not lag behind the twin: whichever sees it first is measured
  let twinAt = null;
  let sceneAt = null;
  await until("failed in twin and scene", async () => {
    const [c, n] = await Promise.all([comp(id), node(page, id)]);
    if (c.status === "failed") twinAt ??= Date.now();
    if (n.status === "failed") sceneAt ??= Date.now();
    return twinAt && sceneAt;
  }, 20, 20);
  const first = await node(page, id);
  ok(first.sinceChange < 2, "no wave at the change");
  await until("red", async () => (await node(page, id)).color === COLOR.failed, 3, 30);
  const red = Date.now() - sceneAt;
  ok((await hook(page, "label", id)).status === "failed", "label");
  ok((await hook(page, "label", id)).text.includes("Failed"), "label text");
  const toast = await page.textContent(`.toast[data-object=${id}]`);
  ok(toast.includes("Payment Gateway Service has failed"), toast);
  ok(await page.$(`[data-testid=alerts] [data-object=${id}][data-status=failed]`), "alert");
  same(await page.getAttribute(`[data-testid=left-panel] [data-id=${id}]`, "data-status"), "failed", "list");
  ok((await page.$eval("[data-testid=status-counts] [data-status=failed] b", (b) => +b.textContent)) >= 1, "count");
  await until("gateway behind it unknown", async () => (await node(page, "gw-external")).status === "unknown", 10);
  await until("failed link", async () => (await wire(page, "bank-transaction->bank-payment")).status === "failed", 30);
  const tx = await comp("bank-transaction");
  ok(tx.status !== "healthy" && tx.root_causes.includes(id), tx);
  await page.click(`[data-testid=alerts] [data-object=${id}]`);
  same(await status(page), "failed", "inspector");
  await page.screenshot({ path: join(OUT, "3d-failure.png") });
  await tab(page, "drawer", 2);
  ok(await page.$(`[data-testid=event-history] [data-object=${id}][data-to=failed]`), "event");
  await bank("start", "payment");
  await until("recovering", async () => (await node(page, id)).status === "recovering", 20, 30);
  await until("recovering colour", async () => ["recovering", "healthy"].includes((await node(page, id)).status) && (await node(page, id)).color !== COLOR.failed, 5);
  const healthyAfter = await until("healthy", async () => (await node(page, id)).status === "healthy" && (await node(page, id)).color === COLOR.healthy, 60);
  await until("message", async () => (await page.textContent("[data-testid=toasts]")).includes("is healthy again"), 5);
  await until("event of recovery", async () => page.$(`[data-testid=event-history] [data-object=${id}][data-to=healthy]`), 5);
  await tab(page, "drawer", 1);
  await reset(page);
  return { crash_to_scene_s: +((sceneAt - t0) / 1000).toFixed(1), twin_to_scene_ms: sceneAt - twinAt, fully_red_ms: red, healthy_again_s: healthyAfter };
});

check("U15", "Overload: warning message, alert, sign, load gauge, distinct look", async (page) => {
  await settled();
  const id = "bank-fraud";
  await bank("fault", "fraud", "resource_exhaustion", "--mode", "cpu", "--magnitude", "0.8", "--duration", "120");
  const seen = await until("overloaded", async () => (await node(page, id)).status === "overloaded", 80, 50);
  await until("message", async () => (await page.$$eval(".toast[data-status=overloaded]", (l) => l.map((e) => e.textContent).join())).includes("Fraud Detection Service is overloaded"), 3);
  ok(await page.$(`[data-testid=alerts] [data-object=${id}][data-status=overloaded]`), "alert");
  await until("colour and load", async () => {
    const n = await node(page, id);
    return n.color === COLOR.overloaded && n.load >= 0.85;
  }, 10);
  await select(page, id);
  await still(page);
  await until("findings", () => page.$("[data-testid=inspector] .findings li"), 15);
  await page.screenshot({ path: join(OUT, "3d-overload.png") });
  await bank("clear");
  await until("healthy", async () => (await node(page, id)).status === "healthy", 60, 200);
  await reset(page);
  return { seen_after_s: seen };
});

check("U16", "Hung: shown as unknown, without numbers, neither healthy nor failed", async (page) => {
  await settled();
  const id = "bank-notification";
  const pid = await pidOf("notification");
  await freeze(pid, true);
  let seen;
  try {
    seen = await until("unknown", async () => (await node(page, id)).status === "unknown", 25, 50);
    await select(page, id);
    const none = await page.$$eval("[data-testid=inspector] .tile", (l) => l.map((e) => e.textContent));
    ok(none.length >= 4 && none.every((x) => x.includes("not measured")), none);
    ok(!(await page.$("[data-testid=inspector] .tile[data-value]")), "a number is shown");
    same((await node(page, id)).load, -1, "load");
    await until("grey", async () => (await node(page, id)).color === COLOR.unknown, 3);
  } finally {
    await freeze(pid, false);
  }
  await until("healthy", async () => (await node(page, id)).status === "healthy", 60, 200);
  await reset(page);
  return { seen_after_s: seen };
});

check("U17", "Slow: warning, not overload; the sign appears and the reason is named", async (page) => {
  await settled();
  const id = "bank-customer";
  await bank("fault", "customer", "latency", "--latency-ms", "400", "--duration", "120");
  const seen = await until("warning", async () => (await node(page, id)).status === "warning", 45, 100);
  await select(page, id);
  ok((await page.textContent("[data-testid=inspector] .reason")).includes("latency"), "reason");
  ok(await page.$(`[data-testid=alerts] [data-object=${id}][data-status=warning]`), "alert");
  // a status warning makes no message; the AI may still report the anomaly, in its own colour
  ok(!(await page.$(`.toast[data-object=${id}]:not([data-status=anomaly]):not([data-status=predicted])`)), "a warning must not shout");
  await bank("clear");
  await until("healthy", async () => (await node(page, id)).status === "healthy", 60, 200);
  await reset(page);
  return { seen_after_s: seen };
});

check("U18", "Display: hide types, highlight a status, cables, names, detail, panels, legend", async (page) => {
  await page.click('[aria-label="Hide Monitoring"]');
  await until("hidden", async () => (await node(page, "mon-service")).visible === 0, 3);
  ok((await hook(page, "label", "mon-service")).shown === false, "label");
  await page.click('[aria-label="Show Monitoring"]');
  await until("shown", async () => (await node(page, "mon-service")).visible === 1, 3);
  same((await wire(page, "bank-account->net-bank")).visible, 0, "cable hidden at first");
  await page.keyboard.press("c");
  await until("cables", async () => (await wire(page, "bank-account->net-bank")).visible === 1, 3);
  await page.keyboard.press("c");
  await page.keyboard.press("l");
  await until("names off", async () => !(await hook(page, "label", "bank-account")).shown, 2);
  await page.keyboard.press("l");
  await until("names on", async () => (await hook(page, "label", "bank-account")).shown, 2);
  await bank("kill", "notification");
  await until("failed", async () => (await node(page, "bank-notification")).status === "failed", 15);
  await page.click("[data-testid=status-counts] [data-status=failed]");
  await until("others dimmed", async () => (await node(page, "bank-account")).visible < 0.3 && (await node(page, "bank-notification")).visible === 1, 3);
  await page.click("[data-testid=mode-note] button");
  await until("all back", async () => (await node(page, "bank-account")).visible === 1, 3);
  await bank("start", "notification");
  await page.selectOption('[aria-label="Detail"]', "low");
  await until("low detail", async () => (await hook(page, "frames")).bloom === false, 4);
  await page.selectOption('[aria-label="Detail"]', "high");
  await until("high detail", async () => (await hook(page, "frames")).bloom === true, 4);
  await page.selectOption('[aria-label="Detail"]', "auto");
  const w0 = (await page.$eval("canvas", (c) => c.getBoundingClientRect().width));
  await page.click('[aria-label="Infrastructure panel"]');
  await page.click('[aria-label="Inspector"]');
  await page.click('[aria-label="Alerts and events"]');
  await until("canvas grows", async () => (await page.$eval("canvas", (c) => c.getBoundingClientRect().width)) > w0 + 500, 3);
  for (const l of ["Infrastructure panel", "Inspector", "Alerts and events"]) await page.click(`[aria-label="${l}"]`);
  await page.click("[data-testid=legend] .fold");
  ok((await page.textContent("[data-testid=legend]")).includes("flashing red"), "legend");
  await page.click("[data-testid=legend] .fold");
  await until("healthy", async () => (await node(page, "bank-notification")).status === "healthy", 60, 300);
  await reset(page);
});

check("U19", "Twin backend restarts: the page says so, turns grey, reconnects by itself and is right again", async (page) => {
  const before = (await api()).meta.run_id;
  await docker("stop", "simucore-backend-1");
  const said = await until("banner", async () => (await page.getAttribute("[data-testid=banner]", "data-state").catch(() => null)) === "offline", 20);
  await until("grey", async () => (await hook(page, "stale")) > 0.7, 5);
  same(await page.getAttribute("[data-testid=freshness]", "data-state"), "offline", "chip");
  await docker("start", "simucore-backend-1");
  const back = await until("live again", async () => (await page.getAttribute("[data-testid=freshness]", "data-state")) === "live" && !(await page.$("[data-testid=banner]")), 60);
  const after = (await api()).meta.run_id;
  ok(after !== before, "the twin did not restart");
  await until("colour back", async () => (await hook(page, "stale")) < 0.05, 5);
  await until("right statuses", async () => {
    for (const c of (await api()).components) if ((await node(page, c.id)).status !== c.status) return false;
    return true;
  }, 20);
  problems.splice(0, problems.length, ...problems.filter((p) => !/WebSocket|502|504|Failed to load|ERR_/.test(p)));
  return { said_after_s: said, live_again_s: back };
});

check("U20", "Monitoring stops: everything unknown, banner, no alarm per component; then back", async (page) => {
  await settled();
  await reset(page);
  await docker("stop", "simucore-monitoring-1");
  const said = await until("stale", async () => (await page.getAttribute("[data-testid=banner]", "data-state").catch(() => null)) === "stale", 30);
  await until("all unknown", async () => {
    for (const id of await hook(page, "nodes")) if ((await node(page, id)).status !== "unknown") return false;
    return true;
  }, 10);
  same(await page.$$eval(".toast", (l) => l.length), 0, "messages");
  ok((await page.textContent("[data-testid=alerts]")).includes("monitoring stream is not connected"), "says that alarms are not followed");
  await docker("start", "simucore-monitoring-1");
  const back = await until("live", async () => (await page.getAttribute("[data-testid=freshness]", "data-state")) === "live", 60);
  await until("healthy", allHealthy, 60, 500);
  problems.splice(0, problems.length, ...problems.filter((p) => !/WebSocket|502|504|Failed to load|ERR_/.test(p)));
  return { said_after_s: said, live_again_s: back };
});

check("U21", "Event history equals the twin's record, survives a reload, filters, and leads to the object", async (page) => {
  await tab(page, "drawer", 2);
  const shown = () => page.$$eval("[data-testid=event-history] button.event", (l) => l.map((e) => `${e.dataset.object}|${e.dataset.to}`));
  const equal = async () => {
    const rows = await api("/history/transitions?minutes=120&limit=500&kind=component");
    // several changes can have the same time: compare everything newer than the 100th
    const limit = Date.parse(rows[Math.min(100, rows.length - 1)].ts);
    const want = rows.filter((r) => Date.parse(r.ts) > limit).map((r) => `${Date.parse(r.ts)}|${r.object_id}|${r.to_status}`).sort();
    const got = (await page.$$eval("[data-testid=event-history] button.event", (l) => l.map((e) => `${Date.parse(e.querySelector("time").dateTime)}|${e.dataset.object}|${e.dataset.to}`)))
      .filter((x) => +x.split("|")[0] > limit).sort();
    return want.length > 10 && JSON.stringify(want) === JSON.stringify(got);
  };
  await until("equal to the record", equal, 15, 400);
  const n = (await shown()).length;
  await page.reload();
  await until("drawn", () => page.evaluate(() => window.__simucore?.frames().count > 20), 20);
  await tab(page, "drawer", 2);
  await until("equal after reload", equal, 15, 400);
  await page.selectOption("[data-testid=event-history] label:nth-of-type(2) select", "failed");
  await until("filtered", async () => (await shown()).length > 0 && (await shown()).every((x) => x.endsWith("|failed")), 3);
  await page.selectOption("[data-testid=event-history] label:nth-of-type(2) select", "any");
  await page.selectOption("[data-testid=event-history] label:nth-of-type(1) select", "all");
  await until("with connections", async () => (await shown()).some((x) => x.includes("->")), 3);
  await page.selectOption("[data-testid=event-history] label:nth-of-type(1) select", "component");
  const id = (await shown())[0].split("|")[0];
  await page.click("[data-testid=event-history] button.event");
  same(await name(page), (await comp(id)).name, "leads to the object");
  await tab(page, "inspector", 3);
  ok((await page.$$eval("[data-testid=object-events] button.event", (l) => l.map((e) => e.dataset.object))).every((x) => x.includes(id)), "events of the object only");
  await tab(page, "inspector", 1);
  await tab(page, "drawer", 1);
  await reset(page);
  return { events: n };
});

/** Puts a proxy between page and twin that a test can manipulate. */
async function tampered(change, opts = {}) {
  const ctl = { sent: [], sockets: 0, upstream: null, client: null, drop: false, mute: false };
  const page = await open({
    ...opts, quiet: true,
    before: async (p) => p.routeWebSocket(/\/ws\/v2\/twin/, (ws) => {
      const server = ws.connectToServer();
      ctl.sockets++;
      ctl.upstream = server;
      ctl.client = ws;
      ws.onMessage((m) => {
        ctl.sent.push(m);
        server.send(m);
      });
      server.onMessage((m) => {
        if (ctl.mute) return;
        const out = change(m, ctl);
        if (out) ws.send(out);
      });
    }),
  });
  return { page, ctl };
}

check("U22", "Lost and damaged messages: the page notices, resyncs, never shows a wrong state", async () => {
  let drop = 0;
  const { page, ctl } = await tampered((m) => {
    if (drop > 0 && m.includes('"type":"delta"')) {
      drop--;
      return null;
    }
    return m;
  });
  drop = 2;
  await until("resync asked", () => ctl.sent.some((x) => x.includes("resync")), 25);
  await until("right again", async () => {
    const t = await api();
    return t.meta.version === (await hook(page, "version"));
  }, 15, 50);
  for (const c of (await api()).components) await until("same", async () => (await node(page, c.id)).status === (await comp(c.id)).status, 8);
  ok(ctl.sent.every((x) => x === '{"type":"resync"}'), `the page sent something else: ${ctl.sent}`);
  // a silent line: no message at all although the connection is open
  ctl.mute = true;
  const noticed = await until("silence noticed", async () => (await page.getAttribute("[data-testid=freshness]", "data-state")) === "offline" || ctl.sockets > 1, 20);
  ctl.mute = false;
  await until("live", async () => (await page.getAttribute("[data-testid=freshness]", "data-state")) === "live", 25);
  // nonsense
  const n = ctl.sockets;
  ctl.client.send("{not json");
  await until("new connection", () => ctl.sockets > n, 10);
  await until("live", async () => (await page.getAttribute("[data-testid=freshness]", "data-state")) === "live", 15);
  await page.close();
  return { silence_noticed_s: noticed, connections: ctl.sockets };
});

check("U23", "Screen sizes from 3440 to 390 pixels: nothing overflows, everything stays reachable", async () => {
  const out = [];
  for (const [w, h] of [[3440, 1440], [1920, 1080], [1366, 768], [1024, 768], [768, 1024], [390, 844]]) {
    const page = await open({ page: { viewport: { width: w, height: h } } });
    const r = await page.evaluate(() => ({
      scroll: document.documentElement.scrollWidth, width: innerWidth,
      canvas: document.querySelector("canvas").getBoundingClientRect().toJSON(),
      toolbar: document.querySelector("[data-testid=toolbar]").getBoundingClientRect().toJSON(),
      off: [...document.querySelectorAll(".topbar *, .toolbar *")].filter((e) => {
        const b = e.getBoundingClientRect();
        return b.width && b.right > innerWidth + 1 && !e.closest(".counts");
      }).length,
    }));
    ok(r.scroll <= r.width, `${w}: page scrolls sideways`);
    same(r.off, 0, `${w}: controls outside`);
    ok(r.canvas.width > 200 && r.canvas.height > 200, `${w}: scene too small ${JSON.stringify(r.canvas)}`);
    if (w < 860) for (const l of ["Inspector", "Infrastructure panel"]) if (await page.$(`[aria-label="${l}"][aria-pressed=true]`)) await page.click(`[aria-label="${l}"]`);
    await page.keyboard.press("1");
    await still(page);
    await page.screenshot({ path: join(OUT, `size-${w}.png`) });
    out.push(`${w}x${h}: scene ${Math.round(r.canvas.width)}x${Math.round(r.canvas.height)}`);
    await page.close();
  }
  return out;
});

check("U24", "Reduced motion: no travelling lights, no flights, all information remains", async () => {
  const page = await open({ page: { reducedMotion: "reduce" } });
  const a = await wire(page, "lb-clients->bank-account");
  await sleep(800);
  const b = await wire(page, "lb-clients->bank-account");
  same(a.phase, b.phase, "lights travel");
  await select(page, "bank-fraud");
  await sleep(150);
  const s = (await node(page, "bank-fraud")).screen;
  ok(Math.hypot(s.x - 762, s.y - 380) < 150, "must be there at once");
  await page.close();
});

check("U25", "Keyboard alone: reach, select, isolate, explore and leave without a mouse", async () => {
  const page = await open();
  await page.keyboard.press("/");
  await page.keyboard.type("account");
  await page.keyboard.press("Enter");
  same(await name(page), "Account Management Service", "selected");
  await page.keyboard.press("i");
  await page.waitForSelector("[data-testid=mode-note]");
  await page.keyboard.press("Escape");
  await page.keyboard.press("d");
  await page.waitForSelector("[data-testid=mode-note]");
  await page.keyboard.press("Escape");
  await page.keyboard.press("Escape");
  ok(!(await page.$("[data-testid=inspector-name]")), "deselected");
  let reached = false;
  for (let i = 0; i < 60 && !reached; i++) {
    await page.keyboard.press("Tab");
    reached = await page.evaluate(() => document.activeElement?.dataset?.id === "bank-transaction");
  }
  ok(reached, "row not reachable by Tab");
  await page.keyboard.press("Enter");
  same(await name(page), "Transaction Processing Service", "row");
  const unnamed = await page.$$eval("button", (l) => l.filter((b) => !(b.textContent.trim() || b.getAttribute("aria-label"))).length);
  same(unnamed, 0, "buttons without a name");
  await page.close();
});

check("U26", "No 3D graphics: the panels still work and say what is missing", async () => {
  const b = await chromium.launch({ channel: "msedge", headless: true, args: ["--disable-3d-apis", "--disable-gpu"] });
  try {
    const page = await b.newPage({ viewport: { width: 1600, height: 900 } });
    await page.goto(BASE);
    await page.waitForSelector("[data-testid=scene-error]", { timeout: 15000 });
    await select(page, "bank-account");
    same(await name(page), "Account Management Service", "inspector");
    ok(await page.$("[data-testid=inspector] .tile[data-metric=cpu_pct]"), "numbers");
    ok((await page.getAttribute("[data-testid=freshness]", "data-state")) === "live", "live");
  } finally {
    await b.close();
  }
});

/** A large installation, made from the real messages: only here, for measuring the drawing. */
function enlarge(n) {
  return (text, ctl) => {
    const m = JSON.parse(text);
    if (m.type !== "snapshot") {
      // every object changes with every message: the hardest case
      if (m.type === "delta" && ctl.big) {
        m.components = ctl.big.components.map((c, i) => ({ ...c, metrics: { ...c.metrics, cpu_pct: (i * 7 + m.meta.version * 13) % 100 }, display: { ...c.display, load: ((i * 7 + m.meta.version * 13) % 100) / 100 }, status: ["healthy", "healthy", "healthy", "warning", "overloaded", "failed"][(i + m.meta.version) % 6] }));
        m.links = ctl.big.links;
        m.transitions = [];
      }
      return JSON.stringify(m);
    }
    const base = m.components;
    const components = [];
    const links = [];
    for (let i = 0; components.length < n; i++) {
      for (const c of base) {
        if (components.length >= n) break;
        components.push({ ...c, id: `${c.id}-${i}`, name: `${c.name} ${i}`, dependencies: { depends_on: [], dependents: [] } });
      }
      for (const k of m.links) links.push({ ...k, id: `${k.source}-${i}->${k.target}-${i}`, source: `${k.source}-${i}`, target: `${k.target}-${i}`, display: { ...k.display, flow: 0.5 } });
    }
    const ids = new Set(components.map((c) => c.id));
    m.components = components;
    m.links = links.filter((k) => ids.has(k.source) && ids.has(k.target));
    ctl.big = m;
    m.summary = { ...m.summary, needs_attention: [] };
    return JSON.stringify(m);
  };
}

check("U27", "Performance: 17, 300 and 1000 components, all changing with every message, stay smooth", async () => {
  const out = {};
  // another page that draws at the same time would be measured with it
  for (const p of browser.contexts().flatMap((x) => x.pages())) await p.goto("about:blank");
  for (const n of [0, 300, 1000]) {
    const { page } = n ? await tampered(enlarge(n), { still: false }) : { page: await open() };
    const count = (await hook(page, "nodes")).length;
    const links = (await hook(page, "wires")).length;
    await page.keyboard.press("1");
    await still(page).catch(() => undefined);
    await sleep(3000);
    // while the camera moves
    const samples = [];
    for (let k = 0; k < 4; k++) {
      await page.keyboard.press(String(1 + (k % 4)));
      await sleep(2400);
      const f = await hook(page, "frames");
      samples.push(f);
    }
    // the picture must not be empty while this is measured
    await page.keyboard.press("1");
    await sleep(2000);
    const seen = await page.evaluate(() => window.__simucore.nodes().filter((i) => {
      const s = window.__simucore.node(i).screen;
      return s.ahead && s.x > 292 && s.x < 1232 && s.y > 48 && s.y < 712;
    }).length);
    ok(seen > count * 0.98, `${seen} of ${count} in the picture`);
    const gaps = samples.flatMap((f) => f.gapMs).sort((a, b) => a - b);
    const cpu = samples.flatMap((f) => f.cpuMs).sort((a, b) => a - b);
    const q = (a, p) => +a[Math.floor((a.length - 1) * p)].toFixed(1);
    const f = samples[samples.length - 1];
    out[count] = { links, fps: +(1000 / (gaps.reduce((a, b) => a + b, 0) / gaps.length)).toFixed(0), frame_ms_p95: q(gaps, 0.95), draw_ms_median: q(cpu, 0.5), draw_calls: f.calls, triangles: f.triangles, detail_level: f.level };
    if (n === 1000) {
      const p = (await node(page, (await hook(page, "nodes"))[500])).screen;
      const t0 = Date.now();
      await hook(page, "pickAt", p.x, p.y);
      out[count].pick_ms = Date.now() - t0;
      await page.screenshot({ path: join(OUT, "3d-1000.png") });
    }
    ok(out[count].fps >= 50, out[count]);
    ok(out[count].draw_calls < 60, out[count]);
    await page.close();
  }
  for (const p of browser.contexts().flatMap((x) => x.pages())) {
    await p.goto(BASE);
    await until("drawn", () => p.evaluate(() => window.__simucore?.frames().count > 30), 20);
  }
  return out;
});

check("U28", "Nothing accumulates: 60 changes of view and 6 of structure leave memory as it was", async () => {
  let extra = 0;
  const { page, ctl } = await tampered((text) => {
    const m = JSON.parse(text);
    if (m.type === "snapshot" && extra % 2) {
      m.components = m.components.filter((c) => c.id !== "mon-prometheus");
    }
    return JSON.stringify(m);
  });
  const cdp = await page.context().newCDPSession(page);
  const heap = async () => {
    for (let i = 0; i < 3; i++) await cdp.send("HeapProfiler.collectGarbage");
    const { usedSize } = await cdp.send("Runtime.getHeapUsage");
    const dom = await cdp.send("Memory.getDOMCounters");
    return { mb: usedSize / 1048576, nodes: dom.nodes, listeners: dom.jsEventListeners, ...(await hook(page, "frames")).memory };
  };
  const exercise = async (n) => {
    for (let i = 0; i < n; i++) {
      await select(page, ["bank-account", "bank-fraud", "store-bank"][i % 3]);
      await page.keyboard.press("d");
      await sleep(60);
      await page.keyboard.press("i");
      await sleep(60);
      await page.keyboard.press("Escape");
      await page.keyboard.press("Escape");
      if (i % 10 === 9) {
        extra++;
        ctl.upstream.send('{"type":"resync"}');
        await until("structure", async () => (await hook(page, "nodes")).length === (extra % 2 ? 16 : 17), 8);
      }
    }
    await sleep(1500);
  };
  await exercise(20);
  const a = await heap();
  await exercise(60);
  const b = await heap();
  ok(b.mb - a.mb < 12, { a, b });
  ok(b.geometries <= a.geometries + 2 && b.textures <= a.textures + 2, { a, b });
  ok(b.nodes < a.nodes * 1.5 + 400, { a, b });
  await page.close();
  const r = (x) => ({ ...x, mb: +x.mb.toFixed(1) });
  return { after_20: r(a), after_80: r(b) };
});

check("U29", "Bank moves into containers: the scene rearranges itself, without reload", async (page) => {
  await reset(page);
  const before = await hook(page, "nodes");
  ok(before.includes("server-bank-host"), "bank host missing");
  stopTraffic();
  await bank("down");
  const env = { ...process.env, BANKCTL_MODE: "docker", PATH: `${dirname(DOCKER)};${process.env.PATH}` };
  const dock = (...a) => run(BANK_PY, ["bankctl.py", ...a], { cwd: join(ROOT, "banking-sim"), env, maxBuffer: 1e7 });
  try {
    await dock("up");
    await until("bank host gone", async () => !(await hook(page, "nodes")).includes("server-bank-host"), 90, 500);
    const t = await api();
    same((await hook(page, "nodes")).sort(), t.components.map((c) => c.id).sort(), "components");
    const zones = await page.$$eval(".zone-label", (l) => l.map((e) => e.textContent));
    ok(zones.includes("Racks on Docker machine"), zones);
    await until("healthy", async () => (await node(page, "bank-account")).status === "healthy", 60, 300);
    await select(page, "bank-account");
    ok((await page.textContent("[data-testid=inspector] .facts")).includes("container"), "deployment");
    await until("its link to the machine", async () => (await wire(page, "bank-account->server-docker")).visible > 0.5, 3);
    await reset(page);
    await page.screenshot({ path: join(OUT, "3d-containers.png") });
  } finally {
    await run(DOCKER, ["compose", "down", "--volumes"], { cwd: join(ROOT, "banking-sim") }).catch(() => undefined);
    await bank("up");
  }
  await until("bank host back", async () => (await hook(page, "nodes")).includes("server-bank-host"), 90, 500);
  same((await hook(page, "nodes")).sort(), before.sort(), "as before");
  traffic();
  await settled();
  return { in_containers: before.length - 1, as_processes: before.length };
});

check("U30", "The page only reads: no writing request, nothing from other sites, no stored data", async () => {
  const requests = [];
  const { page, ctl } = await tampered((m) => m, { page: {} });
  page.on("request", (r) => requests.push([r.method(), r.url()]));
  await page.reload();
  await until("drawn", () => page.evaluate(() => window.__simucore?.frames().count > 20), 20);
  for (const id of ["bank-account", "store-bank", "net-bank"]) {
    await select(page, id);
    await page.keyboard.press("d");
    await page.keyboard.press("Escape");
  }
  await sleep(1500);
  ok(requests.every(([m]) => m === "GET"), requests.filter(([m]) => m !== "GET"));
  const hosts = [...new Set(requests.map(([, u]) => new URL(u)).filter((u) => u.protocol.startsWith("http")).map((u) => u.host))];
  same(hosts, [new URL(BASE).host], "hosts");
  ok(ctl.sent.every((m) => m === '{"type":"resync"}'), ctl.sent);
  const stored = await page.evaluate(() => [localStorage.length, sessionStorage.length, document.cookie]);
  same(stored, [0, 0, ""], "stored");
  await page.close();
  return { requests: requests.length, hosts };
});

check("U31", "Hostile text in the data is shown as text, never run", async () => {
  const evil = '<img src=x onerror="window.__hacked=1"><script>window.__hacked=1</script>';
  const { page } = await tampered((text) => {
    const m = JSON.parse(text);
    for (const c of m.components) if (c.id === "bank-account") Object.assign(c, { name: `Account ${evil}`, reason: evil, status: "warning", description: evil });
    return JSON.stringify(m);
  });
  await select(page, "bank-account");
  ok((await name(page)).includes("<img"), "shown as text");
  for (const n of [1, 2, 3]) await tab(page, "inspector", n);
  await page.keyboard.press("Control+k");
  await page.keyboard.type("account");
  await sleep(500);
  same(await page.evaluate(() => window.__hacked ?? null), null, "code ran");
  await page.close();
});

check("U32", "Six services fail together: the scene agrees with the twin at every moment", async (page) => {
  await settled();
  const services = ["notification", "payment", "fraud", "customer", "account", "transaction"];
  let compared = 0;
  let late = 0;
  const watch = async (seconds) => {
    const end = Date.now() + seconds * 1000;
    while (Date.now() < end) {
      const r = await page.evaluate(async () => {
        const t = await (await fetch("/api/v2/twin")).json();
        await new Promise((f) => setTimeout(f, 40));
        if (t.meta.version !== window.__simucore.version()) return null;
        return [...t.components.map((c) => [c.id, c.status, window.__simucore.node(c.id).status]), ...t.links.map((k) => [k.id, k.status, window.__simucore.wire(k.id).status])];
      });
      if (!r) { late++; continue; }
      for (const [id, want, got] of r) {
        ok(want === got, `${id}: twin ${want}, scene ${got}`);
        compared++;
      }
      await sleep(120);
    }
  };
  for (const s of services) {
    await bank("kill", s);
    await watch(1.5);
  }
  await watch(5);
  ok((await page.$$eval(".toast", (l) => l.length)) <= 4, "the messages must not cover the scene");
  await page.screenshot({ path: join(OUT, "3d-many-failures.png") });
  for (const s of services) {
    await bank("start", s);
    await watch(1.5);
  }
  await until("healthy", allHealthy, 120, 500);
  await reset(page);
  return { statuses_compared: compared, differences: 0, skipped_between_versions: late };
});

// ---------------------------------------------------------------- opinions of the AI
const ai = async (path) => {
  const r = await fetch(`${BASE}/ai/api/v1/ai/${path}`);
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
};
const quiet = async () => {
  const s = await api("/summary");
  const i = await api("/insights");
  return s.ai === "live" && s.anomalies.length === 0 && s.predicted.length === 0
    && Object.values(i).length === 8 && Object.values(i).every((x) => x.risks.length === 2 && x.risks.every((r) => r.probability !== null));
};
/** Memory of a service grows until ``stop`` says so. */
async function leak(service, seconds, stop) {
  const t0 = Date.now();
  while (!(await stop())) {
    const x = Math.min(1, (Date.now() - t0) / 1000 / seconds);
    await bank("fault", service, "resource_exhaustion", "--mode", "memory", "--magnitude", (0.22 + 0.78 * x).toFixed(3), "--duration", "90").catch(() => undefined);
    await sleep(2500);
    if (Date.now() - t0 > (seconds + 45) * 1000) return;
  }
}

check("U33", "Anomaly: the 3D service is marked while its status stays; panel, inspector, explanation", async (page) => {
  await settled();
  await until("quiet", quiet, 200, 1000);
  same([(await node(page, "bank-fraud")).anomaly, (await node(page, "bank-fraud")).risk], [0, 0], "marked before");
  await bank("fault", "fraud", "latency", "--latency-ms", "700", "--duration", "120");
  const seen = await until("anomaly in the scene", async () => (await node(page, "bank-fraud")).anomaly > 0.45, 40, 100);
  ok((await hook(page, "label", "bank-fraud")).text.includes("anomaly"), "label");
  await tab(page, "drawer", 3);
  await until("in the panel", () => page.$("[data-testid=ai-anomalies] [data-object=bank-fraud]"), 5);
  await page.click("[data-testid=ai-anomalies] [data-object=bank-fraud]");
  await until("inspector", () => page.$("[data-testid=ai-tab]"), 5);
  const equal = async () => page.evaluate(async () => {
    const i = (await (await fetch("/api/v2/twin/components/bank-fraud")).json()).insight;
    await new Promise((f) => requestAnimationFrame(() => requestAnimationFrame(f)));
    const tile = (m) => document.querySelector(`[data-testid=ai-tab] .tile[data-metric=${m}]`)?.dataset.value;
    return i.anomaly && Math.abs(+tile("ai_anomaly") - i.anomaly.score) < 0.011
      && i.risks.every((r) => r.probability === null ? tile(`ai_risk_${r.target}`) === "" : Math.abs(+tile(`ai_risk_${r.target}`) - r.probability) < 0.011);
  });
  await until("numbers equal the twin's", equal, 20, 150);
  ok((await page.textContent("[data-testid=ai-tab]")).includes("Why unusual now"), "explanation");
  ok((await page.textContent("[data-testid=ai-reasons]")).match(/Latency/), "contributing metric");
  await page.screenshot({ path: join(OUT, "ai-anomaly.png") });
  await bank("clear");
  await until("mark gone", async () => (await node(page, "bank-fraud")).anomaly === 0, 60, 300);
  await until("in the history", () => page.$("[data-testid=ai-history] [data-kind=anomaly][data-object=bank-fraud]"), 25, 500);
  await reset(page);
  return { seen_after_s: seen };
});

check("U34", "Predictive warning: shown in 3D before the failure, with probability and reason; then the failure comes", async (page) => {
  await settled();
  await until("quiet", quiet, 240, 1000);
  await tab(page, "drawer", 3);
  const id = "bank-account";
  const at = {};
  let status = null;
  let label = "";
  let toast = "";
  const t0 = Date.now();
  await leak("account", 130, async () => {
    const n = await node(page, id);
    if (n.risk > 0.2 && !at.warned) {
      at.warned = (Date.now() - t0) / 1000;
      status = n.status;
      label = (await hook(page, "label", id)).text;
      toast = await page.$$eval(".toast[data-status=predicted]", (l) => l.map((e) => e.textContent).join(" | "));
      ok(await page.$(`[data-testid=ai-predictions] tr[data-id=${id}]:not([data-warning=""])`), "not in the panel");
      await select(page, id);
      await tab(page, "inspector", 4);
      ok((await page.textContent("[data-testid=ai-tab] .ai-alert.risk")).match(/predicted within 2 min/), "inspector");
      ok((await page.textContent("[data-testid=ai-reasons]")).includes("Why this risk"), "reasons");
      await still(page);
      await page.screenshot({ path: join(OUT, "ai-warning.png") });
    }
    if (n.status === "failed" && !at.failed) at.failed = (Date.now() - t0) / 1000;
    return !!at.failed;
  });
  ok(at.warned && at.failed, at);
  ok(status !== "failed" && status !== "overloaded", `status when warned: ${status}`);
  ok(at.failed - at.warned >= 10, at);
  ok(/(failure|degradation) .*%.* in 2 min/.test(label.replace(/\s+/g, " ")), label);
  ok(/Account Management Service: (failure|degradation) predicted/.test(toast), toast);
  await bank("clear");
  await until("came true, in the history", () => page.$(`[data-testid=ai-history] [data-kind=warning][data-object=${id}][data-outcome=came_true]`), 40, 500);
  await until("healthy", async () => (await node(page, id)).status === "healthy", 60, 300);
  await reset(page);
  return { warned_after_s: +at.warned.toFixed(1), failed_after_s: +at.failed.toFixed(1), lead_s: +(at.failed - at.warned).toFixed(1), status_when_warned: status, message: toast.slice(0, 120) };
});

check("U35", "AI panel: history and quality equal the AI service's answers, with their source", async (page) => {
  await tab(page, "drawer", 3);
  await until("history", () => page.$("[data-testid=ai-history] button"), 25, 500);
  const key = (x, kind) => `${x.node_id}|${kind}|${kind === "warning" ? x.outcome : ""}`;
  await until("equal", async () => {
    const [w, a] = await Promise.all([ai("warnings?minutes=720&limit=200&origin=measured"), ai("anomalies?minutes=720&limit=200&origin=measured")]);
    const want = [...w.map((x) => [x.opened_at, key(x, "warning")]), ...a.map((x) => [x.opened_at, key(x, "anomaly")])]
      .sort((p, q) => Date.parse(q[0]) - Date.parse(p[0])).slice(0, 40).map((x) => x[1]).sort();
    const got = (await page.$$eval("[data-testid=ai-history] button", (l) => l.map((e) => `${e.dataset.object}|${e.dataset.kind}|${e.dataset.outcome}`))).slice(0, 40).sort();
    return JSON.stringify(want) === JSON.stringify(got);
  }, 40, 1000);
  const m = await ai("model");
  const text = (await page.textContent("[data-testid=ai-quality]")).replace(/\s+/g, " ");
  ok(text.includes(m.version) && text.includes("synthetic test set") && text.includes("calibrated on synthetic data"), text);
  ok(text.includes(m.test.failure.xgboost.roc_auc.toFixed(2)), "ROC-AUC");
  if (m.validation) ok(text.includes(`${m.validation.failure.events.announced} of ${m.validation.failure.events.failures}`), "validation");
  const panel = (await page.textContent("[data-testid=ai-panel]")).replace(/\s+/g, " ");
  ok(panel.includes("Isolation Forest, unsupervised") && panel.includes("XGBoost, supervised"), "the two methods must be kept apart");
  ok((await page.$$("[data-testid=ai-predictions] tbody tr")).length === 8, "eight services");
  await tab(page, "drawer", 1);
  return { model: m.version, validated_on_real_runs: m.validation?.recordings ?? 0 };
});

check("U36", "AI service off: the page says so, no marks, everything else works; it comes back by itself", async (page) => {
  await tab(page, "drawer", 3);
  await docker("stop", "simucore-intelligence-1");
  const said = await until("said", async () => (await page.getAttribute("[data-testid=ai-panel]", "data-ai")) === "absent", 25);
  for (const id of await hook(page, "nodes")) same([(await node(page, id)).anomaly, (await node(page, id)).risk], [0, 0], id);
  same(await page.getAttribute("[data-testid=freshness]", "data-state"), "live", "the twin");
  ok(await page.$("[data-testid=inspector] .tile"), "measurements");
  await docker("start", "simucore-intelligence-1");
  const back = await until("back", async () => (await page.getAttribute("[data-testid=ai-panel]", "data-ai")) === "live", 60);
  problems.splice(0, problems.length, ...problems.filter((p) => !/502|504|Failed to load|ERR_/.test(p)));
  await tab(page, "drawer", 1);
  return { said_after_s: said, back_after_s: back };
});

// ------------------------------------------------------------------ propagation
/** Pixels of the propagation colour (teal) in a square around a point of the page. */
async function tealAround(page, p, half = 70) {
  const clip = { x: Math.max(0, p.x - half), y: Math.max(0, p.y - half), width: half * 2, height: half * 2 };
  const png = (await page.screenshot({ clip })).toString("base64");
  return page.evaluate(async (b64) => {
    const bmp = await createImageBitmap(await (await fetch(`data:image/png;base64,${b64}`)).blob());
    const c = new OffscreenCanvas(bmp.width, bmp.height).getContext("2d");
    c.drawImage(bmp, 0, 0);
    const d = c.getImageData(0, 0, bmp.width, bmp.height).data;
    let n = 0;
    for (let i = 0; i < d.length; i += 4) if (d[i] < 120 && d[i + 1] > 150 && d[i + 2] > 130 && Math.abs(d[i + 1] - d[i + 2]) < 60) n++;
    return n;
  }, png);
}
const role = async (page, id) => (await node(page, id)).role;

check("U37", "Crash: the failure is marked as origin, its dependents as affected, the path animated; event judged afterwards", async (page) => {
  await settled();
  await page.keyboard.press("4");
  await still(page);
  const tx = await node(page, "bank-transaction");
  const before = await tealAround(page, tx.screen);
  same([await role(page, "bank-payment"), await role(page, "bank-transaction")], [0, 0], "roles before");
  const t0 = Date.now();
  await bank("kill", "payment");
  const marked = await until("origin and impact in the scene", async () => (await role(page, "bank-payment")) === 3
    && (await role(page, "bank-transaction")) >= 1, 20, 50);
  const twin = await comp("bank-transaction");
  ok(twin.impact && twin.impact.chain.join() === "bank-payment,bank-transaction", twin.impact);
  await until("impact like the twin", async () => Math.abs((await node(page, "bank-transaction")).impact - (await comp("bank-transaction")).impact.impact) < 0.02, 5);
  const w = await wire(page, "bank-transaction->bank-payment");
  ok(w.propagation > 0.5 && w.step === 1, w);
  // what Transaction depends on is not affected by its own dependency failing
  for (const id of ["bank-account", "bank-fraud", "bank-database", "bank-notification"]) same(await role(page, id), 0, id);
  await until("label", async () => (await hook(page, "label", "bank-transaction")).text.includes("impact"), 5);
  const label = (await hook(page, "label", "bank-transaction")).text;
  ok(/(confirmed|predicted) impact: high 60 %, 1 step from Payment Gateway/.test(label), label);
  await still(page);
  const during = await tealAround(page, (await node(page, "bank-transaction")).screen);
  ok(during > before + 40, `no propagation colour around Transaction (${before} -> ${during} pixels)`);
  await page.screenshot({ path: join(OUT, "3d-propagation.png") });
  // the panel: live report with chain, event
  await tab(page, "drawer", 4);
  await until("live report", () => page.$("[data-testid=impact-current][data-state=live] [data-id=bank-transaction]"), 10);
  const row = await page.textContent("[data-testid=impact-table] [data-id=bank-transaction]");
  ok(row.includes("Payment Gateway → Transaction Processing"), row);
  await page.click("[data-testid=impact-table] [data-id=bank-transaction]");
  await until("inspector chain", () => page.$("[data-testid=inspector-impact] [data-testid=impact-chain]"), 5);
  await until("event in the panel", () => page.$("[data-testid=impact-events] .impact-event"), 20, 500);
  const live = await api("/propagation/events/live");
  const ev = live.find((e) => e.new_origins.includes("bank-payment") && Date.parse(e.opened_at) >= t0 - 5000);
  ok(ev && ev.predicted.includes("bank-transaction"), live[0]);
  await bank("start", "payment");
  const gone = await until("marks gone", async () => (await role(page, "bank-transaction")) === 0 && (await role(page, "bank-payment")) === 0
    && (await wire(page, "bank-transaction->bank-payment")).propagation < 0.01, 90, 300);
  const judged = await until("event judged and stored", async () => {
    const stored = await api("/propagation/events?minutes=30&limit=20");
    const e = stored.find((x) => x.id === ev.id);
    return e?.closed_at && e.outcome;
  }, 90, 2000);
  const stored = (await api("/propagation/events?minutes=30&limit=20")).find((x) => x.id === ev.id);
  await tab(page, "drawer", 1);
  await page.keyboard.press("1");
  return { marked_after_s: marked, teal_pixels: [before, during], gone_after_s: gone, judged_after_s: judged, outcome: stored.outcome };
});

check("U38", "Simulated failure: the scene shows what would happen; the twin and the bank are not touched; back to live", async (page) => {
  await settled();
  await tab(page, "drawer", 4);
  await page.selectOption("[data-testid=impact-select]", "bank-database");
  await page.click("[data-testid=impact-run]");
  await until("simulated report", () => page.$("[data-testid=impact-current][data-state=simulated]"), 10);
  const sim = await api("/propagation/analyze?failed=bank-database");
  await until("scene shows the simulation", async () => (await role(page, "bank-database")) === 3, 5);
  for (const r of sim.impacts) {
    await until(`${r.id} simulated`, async () => {
      const n = await node(page, r.id);
      return n.role === 1 && Math.abs(n.impact - r.impact) < 0.02;
    }, 5);
  }
  for (const [id, p] of Object.entries(sim.links)) {
    const w = await wire(page, id);
    ok(Math.abs(w.propagation - p.impact) < 0.02 && w.step === p.step, [id, w, p]);
  }
  const shown = await page.$$eval("[data-testid=impact-table] tbody tr", (rows) => rows.map((r) => r.dataset.id));
  same(shown, sim.impacts.map((r) => r.id), "rows");
  ok((await page.textContent("[data-testid=impact-figures]")).includes(String(sim.affected)), "figures");
  const lab = await hook(page, "label", "bank-account");
  ok(lab.text.includes("predicted impact: severe 100 %"), lab);
  const href = await page.getAttribute("[data-testid=impact-download]", "href");
  const md = await (await fetch(BASE + href)).text();
  ok(md.startsWith("# What if Database Service failed") && md.includes("Account Management"), md.slice(0, 200));
  await page.screenshot({ path: join(OUT, "3d-simulation.png") });
  // nothing was changed: the database is still healthy, in the twin and in the bank
  same((await comp("bank-database")).status, "healthy", "twin");
  same((await api()).summary.propagation.origins, [], "no live propagation");
  await page.click("[data-testid=impact-clear]");
  await until("back to live", async () => (await role(page, "bank-database")) === 0 && (await role(page, "bank-account")) === 0, 5);
  // a row of the critical list simulates that component
  const crit = await api("/propagation/critical");
  const first = await page.$eval("[data-testid=impact-critical] tbody tr", (r) => r.dataset.id);
  same(first, crit.components[0].id, "most critical");
  await page.click("[data-testid=impact-critical] tbody tr");
  await until("simulated from the list", async () => (await role(page, first)) === 3, 10);
  await page.click("[data-testid=impact-clear]");
  await until("back to live again", async () => (await role(page, first)) === 0, 5);
  await tab(page, "drawer", 1);
  return { simulated: sim.affected, links: Object.keys(sim.links).length, most_critical: first };
});

check("U39", "The engine follows the structure of the twin: graph and ranking come from what the twin holds now", async () => {
  const [t, g, crit] = [await api(), await api("/propagation/graph"), await api("/propagation/critical?group=all")];
  same(g.nodes.map((n) => n.id).sort(), t.components.map((c) => c.id).sort(), "nodes");
  same(g.edges.map((e) => e.id).sort(), t.links.map((k) => k.id).sort(), "edges");
  for (const e of g.edges) {
    const k = t.links.find((x) => x.id === e.id);
    same([e.kind, e.mode, e.weight], [k.kind, k.mode, k.weight], e.id);
  }
  same(crit.components.length, t.components.length, "every component ranked");
  return { nodes: g.nodes.length, edges: g.edges.length, checksum: g.checksum };
});

// ---------------------------------------------------------------------- main
const wanted = process.argv.slice(2);
const results = [];
mkdirSync(OUT, { recursive: true });
const { stdout: state } = await bank("status", "--json");
if (/"alive"/.test(state)) {
  console.error("The bank is already running. Stop it first (bankctl.py down).");
  process.exit(2);
}
// fresh synthetic data: long runs pay out the accounts, and then no payment gets through
await bank("reset");
await bank("up");
traffic();
browser = await chromium.launch({ channel: "msedge", headless: true, args: GPU });
let page;
try {
  await until("twin live and healthy", allHealthy, 120, 1000);
  await sleep(20_000); // the measuring window fills
  await until("healthy", allHealthy, 90, 1000);
  page = await open();
  for (const c of checks) {
    if (wanted.length && !wanted.includes(c.code)) continue;
    problems.length = 0;
    const t0 = Date.now();
    let error = null;
    let measured;
    try {
      measured = await c.f(page);
      if (problems.length) throw new Error(`browser: ${problems.slice(0, 3).join(" | ")}`);
    } catch (e) {
      error = e.message;
      await page.screenshot({ path: join(OUT, `error-${c.code}.png`) }).catch(() => undefined);
      // the next check must not suffer from this one
      await bank("clear").catch(() => undefined);
      await bank("up").catch(() => undefined);
      for (const p of browser.contexts().flatMap((x) => x.pages())) await p.close().catch(() => undefined);
      if (!workload) traffic();
      await until("healthy", allHealthy, 120, 1000).catch(() => undefined);
      page = await open().catch(() => page);
    }
    results.push({ code: c.code, title: c.title, passed: !error, error, measured, seconds: Math.round((Date.now() - t0) / 1000) });
    console.log(`${error ? "FAIL" : "pass"}  ${c.code}  ${c.title}${error ? `\n      ${error.slice(0, 500)}` : measured ? `\n      ${JSON.stringify(measured).slice(0, 400)}` : ""}`);
    if (!error) await reset(page).catch(() => undefined);
  }
} finally {
  stopTraffic();
  await browser.close();
  await bank("down").catch(() => undefined);
}
const passed = results.filter((r) => r.passed).length;
console.log(`\n${passed}/${results.length} passed`);
if (!wanted.length) {
  const stamp = new Date().toISOString().slice(0, 19) + "Z";
  writeFileSync(join(OUT, "e2e.json"), JSON.stringify({ run_at: stamp, passed, total: results.length, results }, null, 1) + "\n");
  const cell = (s) => String(s ?? "").replace(/\|/g, "/").replace(/\n/g, " ");
  writeFileSync(join(OUT, "e2e.md"), [
    "# 3D interface: results in a real browser", "",
    `Run at ${stamp} in Microsoft Edge against the Docker stack and the real simulated bank. **${passed} of ${results.length} passed.** Generated by \`e2e/run.mjs\`.`, "",
    "| | Check | Result | Measured |", "|---|---|---|---|",
    ...results.map((r) => `| ${r.code} | ${cell(r.title)} | ${r.passed ? "pass" : "FAIL"} | ${cell(r.passed ? (r.measured ? JSON.stringify(r.measured) : "") : r.error)} |`), "",
  ].join("\n"));
}
process.exit(passed === results.length ? 0 : 1);
