import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // Lets the app use relative "/api/..." paths in development, exactly as
    // it does in production (where Flask serves the built bundle from the
    // same origin). Without this proxy the dev server would 404 on /api
    // and you'd be forced to hardcode an absolute localhost URL that then
    // breaks once deployed.
    proxy: {
      '/api': {
        target: process.env.VITE_DEV_API_TARGET || 'http://localhost:5000',
        changeOrigin: true,
      },
    },
  },
})
