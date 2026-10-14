import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: { '/v1': { target: 'http://127.0.0.1:8000', changeOrigin: true, timeout: 180000 } },
  },
  test: {
    environment: 'jsdom',
    setupFiles: './src/tests/setup.ts',
    include: ['src/tests/**/*.test.ts?(x)'],
    restoreMocks: true,
  },
});
