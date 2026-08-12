import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    // `npm run dev` proxies the API so the browser sees one origin and the
    // session cookie is sent without CORS gymnastics.
    proxy: {
      "/auth": "http://localhost:8000",
      "/console": "http://localhost:8000",
      "/tasks": "http://localhost:8000",
    },
  },
});
