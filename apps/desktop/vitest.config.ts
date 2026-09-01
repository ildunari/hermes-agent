import type { TestProjectConfiguration } from 'vitest/config'
import { defineConfig } from 'vitest/config'

const reactUi: TestProjectConfiguration = {
  extends: './vite.config.ts',
  test: {
    name: 'ui',
    environment: 'jsdom',
    setupFiles: ['./vitest.setup.ts'],
    // carry.py's stable Desktop test contract routes declared carry tests
    // through the ui project. Keep this one Electron-side carry sentinel here
    // until the verifier supports project-aware Desktop test routing.
    include: [
      'src/**/*.test.{ts,tsx}',
      'electron/update-count.test.ts',
      'electron/ssh-connection.test.ts'
    ],
    globals: true,
    // The first test in each file pays jsdom env init + full module transform,
    // which can exceed vitest's 5000ms default under CI/load. 15s gives the
    // cold start headroom without masking genuinely hung tests.
    testTimeout: 15_000
  }
}

const electronNative: TestProjectConfiguration = {
  test: {
    name: 'electron',
    environment: 'node',
    testTimeout: 15_000,
    // `e2e/**/*.unit.test.ts` is the e2e HELPERS, not the specs: plain node
    // modules that should be provable without booting Electron. Playwright
    // ignores the same pattern so they run in exactly one runner.
    include: ['electron/**/*.test.ts', 'scripts/**.test.{ts,mjs}', 'e2e/**/*.unit.test.ts'],
    exclude: [
      'electron/autoplay-policy.test.ts',
      'electron/runtime-paths.test.ts',
      'electron/ssh-connection.test.ts',
      'electron/update-count.test.ts',
      'scripts/run-short-session-hang-repro.test.mjs'
    ]
  }
}

export default defineConfig({
  test: {
    projects: [reactUi, electronNative]
  }
})
