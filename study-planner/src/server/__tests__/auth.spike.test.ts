import express from 'express';
import { toNodeHandler, fromNodeHeaders } from 'better-auth/node';
import { describe, expect, it } from 'vitest';
import { connect } from '../db';
import { createAuth } from '../auth';

describe('auth spike', () => {
  it('signs up, signs in and reads the session over HTTP', async () => {
    const { db, close } = await connect({ dataDir: ':memory:' });
    const created: string[] = [];
    const auth = createAuth(db, { onUserCreated: async (id) => void created.push(id) });
    const app = express();
    app.all('/api/auth/*splat', toNodeHandler(auth));
    app.get('/api/me', async (req, res) => {
      const s = await auth.api.getSession({ headers: fromNodeHeaders(req.headers) });
      res.json({ user: s?.user?.email ?? null });
    });
    const server = app.listen(0);
    const port = (server.address() as any).port;
    const base = `http://localhost:${port}`;
    process.env.PUBLIC_URL = base;
    const headers = { 'Content-Type': 'application/json', Origin: 'http://localhost:3000' };
    const up = await fetch(`${base}/api/auth/sign-up/email`, { method: 'POST', headers, body: JSON.stringify({ email: 'a@example.com', password: 'correct horse battery', name: 'Ann' }) });
    const upBody = await up.text();
    expect(up.status, upBody).toBe(200);
    expect(created).toHaveLength(1);
    const cookie = up.headers.getSetCookie().map((c) => c.split(';')[0]).join('; ');
    const me = await (await fetch(`${base}/api/me`, { headers: { cookie } })).json();
    expect(me.user).toBe('a@example.com');
    const bad = await fetch(`${base}/api/auth/sign-in/email`, { method: 'POST', headers, body: JSON.stringify({ email: 'a@example.com', password: 'wrong password!!' }) });
    expect(bad.status).toBe(401);
    server.close();
    await close();
  }, 30000);
});
