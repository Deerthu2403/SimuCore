import { useEffect } from "react";
import { connectTwinSocket } from "./services/twinSocket";
import { TwinCanvas } from "./scene/TwinCanvas";
import { TopBar } from "./components/TopBar";
import { ModuleNav } from "./components/ModuleNav";
import { Inspector } from "./components/Inspector";
import { ImpactPanel } from "./components/ImpactPanel";
import { MonitoringPanel } from "./components/MonitoringPanel";
import { connectMonitoring } from "./services/monitoringSocket";
import { Legend } from "./components/Legend";

export default function App() {
  useEffect(() => connectTwinSocket(), []);
  useEffect(() => connectMonitoring(), []);

  return (
    <div className="app">
      <TopBar />
      <main className="stage">
        <TwinCanvas />
        <ModuleNav />
        <div className="right-column">
          <Inspector />
          <ImpactPanel />
          <MonitoringPanel />
        </div>
        <Legend />
      </main>
    </div>
  );
}
