import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The agent panel talks to the Django API. In dev, proxy /api to the backend so
// there are no CORS hoops and the same relative URLs work in production behind a
// reverse proxy. Override the target with VITE_API_TARGET if the backend runs
// elsewhere.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.VITE_API_TARGET || "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
