import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Same stack as the host AW UI (Vite + React 19 + Tailwind v4).
// HMR is proxied through awserv's /app-proxy WS forwarding, so we set
// hmr.clientPort to the public port the iframe loads from.
const PORT = Number(process.env.AW_APP_PORT) || 10021
// Backend runs on a port derived from PORT (shared netns across all
// custom apps — see entrypoint.sh comment).
const BACKEND_PORT = PORT + 10000
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: '0.0.0.0',
    port: PORT,
    strictPort: true,
    allowedHosts: true,
    hmr: {
      clientPort: PORT,
    },
    proxy: {
      // Dashboard REST API.
      '/api': `http://127.0.0.1:${BACKEND_PORT}`,
      // Only /p/<slug>/_frame* goes to the backend (the actual project
      // content, iframed by the SPA). Bare /p/<slug> must fall through to
      // index.html so the SPA's own client-side route renders the iframe
      // shell — a prefix match on '/p' would proxy that route away too.
      '^/p/[^/]+/_frame': `http://127.0.0.1:${BACKEND_PORT}`,
      // Bare /_frame* — reached only via Caddy's per-project subdomain
      // (ux-proto--<slug>.app.{AW_DOMAIN}, see caddy_template.py
      // wildcard_children), which proxies straight to this same Vite port.
      // The dashboard SPA is never loaded on that host, so no fallthrough
      // conflict like the /p/<slug> case above. changeOrigin MUST stay
      // false (Vite's string-shorthand proxy form defaults it to true) —
      // the backend derives the project slug from the original Host
      // header (ux-proto--<slug>.app...), so rewriting it to
      // 127.0.0.1:<port> breaks slug resolution.
      '/_frame': { target: `http://127.0.0.1:${BACKEND_PORT}`, changeOrigin: false },
      '/ws': { target: `ws://127.0.0.1:${BACKEND_PORT}`, ws: true },
    },
  },
})
