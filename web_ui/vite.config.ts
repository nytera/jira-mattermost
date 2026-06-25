import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";

// The SPA is served under /admin by FastAPI (StaticFiles), so every asset URL
// must be prefixed with /admin/. In dev, proxy the JSON API to the running bot
// on :8080 so `npm run dev` talks to a real backend.
export default defineConfig({
  base: "/admin/",
  plugins: [react()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/admin/api": {
        target: "http://localhost:8080",
        changeOrigin: true,
      },
    },
  },
});
