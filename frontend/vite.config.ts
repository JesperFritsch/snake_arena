import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dev server runs on :5173 — matches the CORS_ORIGINS example in the API's
// settings.py. The API base URL comes from VITE_API_BASE_URL (see .env.example).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    fs: {
      // Allow reading the markdown files in ../docs/legal via ?raw imports.
      allow: [".."],
    },
  },
});
