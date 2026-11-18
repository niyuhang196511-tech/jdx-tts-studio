import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
    {
      name: "jdx-dev-session",
      configureServer(server) {
        server.middlewares.use("/__dev/session", async (request, response) => {
          const host = request.headers.host?.split(":")[0];
          const origin = request.headers.origin;
          const allowedOrigin =
            !origin || origin === `http://${request.headers.host}`;
          if (
            !["127.0.0.1", "localhost", "[::1]"].includes(host ?? "") ||
            !allowedOrigin ||
            request.headers["sec-fetch-site"] === "cross-site"
          ) {
            response.statusCode = 403;
            response.end("Forbidden");
            return;
          }
          try {
            const session = JSON.parse(
              await readFile(
                resolve(
                  process.env.VITE_BACKEND_SESSION_FILE ?? ".dev-session.json",
                ),
                "utf8",
              ),
            ) as { base_url?: string; token?: string };
            if (!session.base_url || !session.token)
              throw new Error("Invalid session");
            response.setHeader("Content-Type", "application/json");
            response.setHeader("Cache-Control", "no-store");
            response.setHeader("X-Content-Type-Options", "nosniff");
            response.end(JSON.stringify(session));
          } catch {
            response.statusCode = 503;
            response.setHeader("Content-Type", "application/json");
            response.end(JSON.stringify({ error: "本地服务尚未就绪" }));
          }
        });
      },
    },
  ],
  clearScreen: false,
  server: {
    host: "127.0.0.1", port: 1420, strictPort: false,
    watch: { ignored: ["**/backend/**", "**/build/**", "**/release/**", "**/src-tauri/**", "**/.data/**", "**/test-results/**", "**/playwright-report/**", "**/docs/**"] },
  },
  envPrefix: ["VITE_", "TAURI_ENV_*"],
  build: {
    target: ["es2022", "chrome105", "safari13"],
    rollupOptions: {
      output: { manualChunks: { ui: ["react", "react-dom", "@tanstack/react-query"], waveform: ["wavesurfer.js"] } },
    },
  },
});
