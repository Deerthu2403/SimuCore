import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const backend = process.env.SIMUCORE_BACKEND_URL ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: { manualChunks: { three: ["three"], r3f: ["@react-three/fiber", "@react-three/drei"] } },
    },
    chunkSizeWarningLimit: 900,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": backend,
      "/ws": { target: backend.replace(/^http/, "ws"), ws: true },
    },
  },
});
