import { Component, useEffect, type ReactNode } from "react";
import { TwinScene } from "./scene/TwinScene";
import { connectMonitoring } from "./twin/monitoring";
import { startTwin, useTwin } from "./twin/store";
import { useView } from "./twin/view";
import { Banner, Drawer, Legend, ModeNote, Search, Toasts, Toolbar, TopBar } from "./ui/Chrome";
import { Inspector } from "./ui/Inspector";
import { LeftPanel } from "./ui/LeftPanel";

export default function App() {
  useEffect(startTwin, []);
  useEffect(connectMonitoring, []);
  useKeys();
  const { left, right, bottom } = useView();
  return (
    <div className={`app ${left ? "has-left" : ""} ${right ? "has-right" : ""} ${bottom ? "has-bottom" : ""}`}>
      <TopBar />
      {left && <LeftPanel />}
      <main className="stage" aria-label="3D scene">
        <SceneGuard><TwinScene /></SceneGuard>
        <Waiting />
        <Banner />
        <ModeNote />
        <Legend />
        <Toolbar />
        <Toasts />
      </main>
      {right && <Inspector />}
      {bottom && <Drawer />}
      <Search />
    </div>
  );
}

function Waiting() {
  const ready = useTwin((s) => !!s.mirror);
  return ready ? null : <div className="waiting"><span className="spinner" />Waiting for the digital twin…</div>;
}

/** Without 3D graphics the lists, measurements and alerts still work. */
class SceneGuard extends Component<{ children: ReactNode }, { error: string | null }> {
  state = { error: null as string | null };
  static getDerivedStateFromError(e: unknown) {
    return { error: e instanceof Error ? e.message : String(e) };
  }
  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="scene-error" role="alert" data-testid="scene-error">
        <b>The 3D scene cannot be shown.</b>
        <span>{this.state.error}</span>
        <span>The panels keep working without it.</span>
      </div>
    );
  }
}

/** Keyboard: everything the mouse can do. */
function useKeys(): void {
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      const v = useView.getState();
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        return v.set({ searchOpen: !v.searchOpen });
      }
      const t = e.target as HTMLElement;
      if (v.searchOpen || e.ctrlKey || e.metaKey || e.altKey || t.closest("input, select, textarea")) return;
      const selected = v.selected;
      const preset = { "1": "overview", "2": "front", "3": "side", "4": "top" }[e.key] as "overview" | undefined;
      if (preset) return v.request({ what: "preset", name: preset });
      const step = (e.shiftKey
        ? { ArrowLeft: "panLeft", ArrowRight: "panRight", ArrowUp: "panUp", ArrowDown: "panDown" }
        : { ArrowLeft: "left", ArrowRight: "right", ArrowUp: "up", ArrowDown: "down" })[e.key] as "left" | undefined;
      if (step) {
        e.preventDefault();
        return v.request({ what: "step", name: step });
      }
      switch (e.key) {
        case "/": e.preventDefault(); return v.set({ searchOpen: true });
        case "+": case "=": return v.request({ what: "step", name: "zoomIn" });
        case "-": return v.request({ what: "step", name: "zoomOut" });
        case "c": case "C": return v.set({ structural: !v.structural });
        case "l": case "L": return v.set({ labels: !v.labels });
        case "r": case "R": return v.reset();
        case "f": case "F":
          if (selected?.kind === "component") v.request({ what: "fit", ids: [selected.id] });
          return;
        case "i": case "I": return v.setMode(v.mode === "isolate" ? "normal" : "isolate");
        case "d": case "D": return v.setMode(v.mode === "dependencies" ? "normal" : "dependencies");
        case "Escape":
          // step by step: first the mode, then the selection
          if (v.mode !== "normal") return v.setMode("normal");
          if (v.statusFilter) return v.setStatusFilter(null);
          return v.select(null);
      }
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, []);
}
