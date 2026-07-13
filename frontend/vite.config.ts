import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev: Vite serves the SPA on 5173 and proxies data + chat to the FastAPI
// backend on 8765. Prod: `npm run build` and the backend serves dist/ itself.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8765',
      '/ws': { target: 'ws://127.0.0.1:8765', ws: true },
    },
  },
})
