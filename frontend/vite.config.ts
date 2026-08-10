import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The build lands inside the Python package rather than in frontend/dist, so a
// wheel carries the portal with it and `matienzo-web` serves a finished site
// with no separate deploy step. `emptyOutDir` is safe because nothing but the
// build writes there — see the .gitignore entry beside it.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../matienzo/web/static",
    emptyOutDir: true,
    sourcemap: true,
  },
  server: {
    // `just frontend-dev` runs Vite in front of a locally running portal. Only
    // the API is proxied; everything else is served by Vite so hot reload works.
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/healthz": "http://127.0.0.1:8000",
    },
  },
});
