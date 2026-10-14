import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // must match `config.API_CORS_ORIGINS` in backend/bridge/config.py exactly —
    // that list is origin-exact-string-matched, so this can't drift to 127.0.0.1.
    host: 'localhost',
    port: 5173,
    strictPort: true,
  },
})
