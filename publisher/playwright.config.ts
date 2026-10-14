import { defineConfig, devices } from '@playwright/test';
import { join } from 'node:path';
const evidence = process.env.GATEWAY_OPERATIONS_EVIDENCE_DIR ?? 'test-results';
/** No mock gateway or automatic substitute server: this suite is a real-runtime gate. */
export default defineConfig({
  testDir: '../tests/e2e',
  testMatch: 'publisher.spec.ts',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 180000,
  expect: { timeout: 15000 },
  reporter: [
    ['list'],
    ['json', { outputFile: join(evidence, 'publisher-results.json') }],
    ['junit', { outputFile: join(evidence, 'publisher-junit.xml') }],
  ],
  outputDir: 'test-results/browser',
  use: {
    baseURL: process.env.GATEWAY_E2E_BASE_URL,
    trace: 'off',
    screenshot: 'off',
    video: 'off',
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH }
      : {},
  },
  projects: [{ name: 'chromium-real-gateway', use: { ...devices['Desktop Chrome'] } }],
});
