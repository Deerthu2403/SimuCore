import { useEffect } from "react";
import { connectTwinSocket } from "./services/twinSocket";
import { TwinCanvas } from "./scene/TwinCanvas";
import { TopBar } from "./components/TopBar";
import { ModuleNav } from "./components/ModuleNav";
import { Inspector } from "./components/Inspector";
import { ImpactPanel } from "./components/ImpactPanel";
import { Legend } from "./components/Legend";

export default function App() {
  useEffect(() => connectTwinSocket(), []);

  return (
    <div className="app">
      <TopBar />
      <main className="stage">
        <TwinCanvas />
        <ModuleNav />
        <div className="right-column">
          <Inspector />
          <ImpactPanel />
        </div>
        <Legend />
      </main>
    </div>
  );
}
