import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The SPA is built into web/dist and served same-origin by Sali's FastAPI in production. In dev, Vite
// runs on :5173 and proxies the API + both WebSockets to the running `sali serve --host 127.0.0.1 --port 8790`.
const BACKEND = 'http://127.0.0.1:8790'
const WS_BACKEND = 'ws://127.0.0.1:8790'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': BACKEND,
      '/health': BACKEND,
      '/ws': { target: WS_BACKEND, ws: true },
      '/stream': { target: WS_BACKEND, ws: true },
    },
  },
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 1600, // three.js is large; that's expected for a single-bundle local app
  },
})
