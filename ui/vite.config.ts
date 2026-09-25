import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

import { API_SERVER_PORT, APP_DEV_PORT } from "./shared/ports.ts";

// `base: "./"` keeps the built bundle path-relative, so the same dist/ can be served by
// the Hono server, opened from disk, or packaged into a desktop webview later.
export default defineConfig({
	base: "./",
	plugins: [react()],
	build: { outDir: "dist", emptyOutDir: true },
	server: {
		port: APP_DEV_PORT,
		strictPort: true,
		proxy: {
			"/api": { target: `http://127.0.0.1:${API_SERVER_PORT}`, changeOrigin: false },
		},
	},
});
