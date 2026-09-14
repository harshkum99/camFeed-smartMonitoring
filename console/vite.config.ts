import { fileURLToPath } from "node:url"
import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"
import tailwindcss from "@tailwindcss/vite"

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  build: { outDir: "dist", emptyOutDir: true },
  server: {
    port: 5173,
    // The API is served by FastAPI; proxying in dev keeps the fetch paths identical to
    // production, where both come off the same origin.
    proxy: { "/api": "http://localhost:8420" },
  },
})
