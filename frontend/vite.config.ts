import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev server proxies the API to a locally running backend (make up exposes it through nginx on :8080).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://localhost:8080",
      "/ws": { target: "ws://localhost:8080", ws: true },
    },
  },
});
