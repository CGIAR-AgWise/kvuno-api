import { defineConfig, loadEnv } from 'vite'
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
export default defineConfig(({ mode }) => {
  // The dev proxy target is configurable because the API is reachable on
  // different ports depending on how it is running: the compose `api` service
  // publishes host 5000 -> container 80, but a bare `python run.py` listens on
  // 80 directly. Default to the compose mapping.
  const env = loadEnv(mode, process.cwd(), '')
  const target =
    env.VITE_DEV_PROXY_TARGET || process.env.VITE_DEV_PROXY_TARGET || 'http://127.0.0.1:5000'

  return {
    plugins: [react()],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
    server: {
      port: Number(env.VITE_DEV_PORT || 5173),
      proxy: {
        '/api': { target, changeOrigin: true },
        // The tus upload endpoints (/ui/uploads/*), /ui/process, /ui/columns
        // and the jobs SSE stream still live on the API container even though
        // the HTML pages do not.
        '/ui': { target, changeOrigin: true },
        '/health': { target, changeOrigin: true },
      },
    },
    build: {
      outDir: 'dist',
      emptyOutDir: true,
      // 'hidden': maps are still written for debugging a build locally, but
      // bundles carry no sourceMappingURL and nginx refuses *.map, so the
      // original source is not published alongside the app.
      sourcemap: 'hidden',
    },
  }
})
