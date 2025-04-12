import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { unstable_dev } from 'wrangler';
import type { UnstableDevWorker } from 'wrangler';

describe('API Tests', () => {
  let worker: UnstableDevWorker;

  beforeAll(async () => {
    worker = await unstable_dev('src/index.ts', {
      experimental: { disableExperimentalWarning: true },
      vars: {
        DEBUG_MODE: 'true',
        JWT_SECRET: 'test-secret',
      },
    });
  });

  afterAll(async () => {
    await worker.stop();
  });

  it('should return the correct API info', async () => {
    const resp = await worker.fetch('/');
    expect(resp.status).toBe(200);

    const data = await resp.json();
    expect(data).toHaveProperty('name', 'Blawby Gmail Agent API');
    expect(data).toHaveProperty('version');
    expect(data).toHaveProperty('status', 'operational');
  });

  it('should handle missing authorization header', async () => {
    const resp = await worker.fetch('/gmail/classify', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        email: {
          subject: 'Test Email',
          body: 'Test body',
          from: 'test@example.com',
        },
      }),
    });

    expect(resp.status).toBe(401);
    const data = await resp.json();
    expect(data).toHaveProperty('error', 'Unauthorized');
  });

  it('should verify token expiration', async () => {
    // Create an expired token
    const expiredToken = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ1c2VyLXRlc3QiLCJpYXQiOjE2MTQ1NTYwMDAsImV4cCI6MTYxNDU1NjAwMH0.8y7oZWd9FcuOEGpGcIxxCBODSxZV3XcnUTLGGxgp7FE';

    const resp = await worker.fetch('/gmail/classify', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${expiredToken}`,
      },
      body: JSON.stringify({
        email: {
          subject: 'Test Email',
          body: 'Test body',
          from: 'test@example.com',
        },
      }),
    });

    expect(resp.status).toBe(401);
    const data = await resp.json();
    expect(data).toHaveProperty('error', 'Invalid token');
  });
}); 