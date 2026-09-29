import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath, URL } from 'node:url'

/**
 * The React app is a standalone SPA deployed behind its own nginx (see
 * frontend/Dockerfile + nginx.conf). It is NOT served by Flask — the API
 * container exposes only JSON and the handful of /ui/* endpoints the upload
 * and SSE flows still need.
 *
 * `vite dev` proxies /api, /ui, and /health to the Flask container so local
 * development is same-origin and needs no CORS; in production nginx does the
 * same proxying.
 */
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://localhost:80', changeOrigin: true },
      // The resumable-upload endpoints, /ui/process, /ui/columns and the jobs
      // SSE stream still live on the API container even though the HTML pages
      // do not.
      '/ui': { target: 'http://localhost:80', changeOrigin: true },
      '/health': { target: 'http://localhost:80', changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: true,
  },
})
