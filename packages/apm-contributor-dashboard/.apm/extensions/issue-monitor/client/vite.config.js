import { defineConfig } from "vite";
import solidPlugin from "vite-plugin-solid";

export default defineConfig({
  plugins: [solidPlugin()],
  build: {
    outDir: "../dist",
    emptyOutDir: true,
    target: "esnext",
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:3456",
      "/start-session": "http://127.0.0.1:3456",
      "/open-session": "http://127.0.0.1:3456",
      "/run-panel": "http://127.0.0.1:3456",
      "/approve-pipeline": "http://127.0.0.1:3456",
      "/approve-pr": "http://127.0.0.1:3456",
      "/approve-workflow-runs": "http://127.0.0.1:3456",
      "/merge-when-ready": "http://127.0.0.1:3456",
    },
  },
});
