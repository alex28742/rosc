import { defineConfig } from 'vitest/config';

// The API proxy target is configurable but must stay on the local machine.
const target = process.env.ROSC_API_TARGET ?? 'http://127.0.0.1:8000';
const host = new URL(target).hostname;
if (!['127.0.0.1', 'localhost', '[::1]'].includes(host)) {
  throw new Error(`ROSC_API_TARGET must point to localhost, got ${host}`);
}

export default defineConfig({
  server: { proxy: { '/api': target } },
  preview: { proxy: { '/api': target } },
  test: { environment: 'jsdom', include: ['src/**/*.test.{ts,tsx}'] },
});
