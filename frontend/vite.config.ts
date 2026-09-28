import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Where the dev server forwards API calls. Override with VITE_API_TARGET.
declare const process: { env: Record<string, string | undefined> }
const apiTarget = process.env.VITE_API_TARGET || 'http://127.0.0.1:8010'
const proxy = {
  '/api': { target: apiTarget, changeOrigin: true },
  '/health': { target: apiTarget, changeOrigin: true },
}

export default defineConfig({
  plugins: [react()],
  server: { proxy },
  preview: { proxy },
})
