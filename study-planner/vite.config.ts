import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  root: '.',
  build: { outDir: 'dist', emptyOutDir: true },
  server: { port: 5173, proxy: { '/api': 'http://localhost:3000', '/oauth': 'http://localhost:3000' } },
  test: { include: ['src/**/*.test.ts'] },
} as any);
