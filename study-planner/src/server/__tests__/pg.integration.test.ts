/**
 * Runs the API flow against a real Postgres (the production driver) when TEST_DATABASE_URL is set.
 * Skipped otherwise; the PGlite suite covers the same flow.
 */
import { describe, expect, it } from 'vitest';
import { sql } from 'drizzle-orm';
import { connect } from '../db';
import { createAuth } from '../auth';
import { createApp } from '../app';
import { ensureProfile } from '../repo';
import { tick } from '../jobs';

const url = process.env.TEST_DATABASE_URL;

describe.skipIf(!url)('real Postgres', () => {
  it('migrates, signs up, imports demo data, serializes writes, syncs in the background and deletes', async () => {
    const { db, close, kind } = await connect({ url });
    expect(kind).toBe('postgres');
    const auth = createAuth(db, { onUserCreated: (id) => ensureProfile(db, id) });
    const server = createApp({ db, auth }).listen(0);
    const base = `http://localhost:${(server.address() as any).port}`;
    const H = { 'Content-Type': 'application/json', Origin: 'http://localhost:3000' };
    const email = `pg${Date.now()}@example.com`;
    const up = await fetch(`${base}/api/auth/sign-up/email`, { method: 'POST', headers: H, body: JSON.stringify({ email, password: 'a long enough password', name: 'PG' }) });
    expect(up.status).toBe(200);
    const cookie = up.headers
      .getSetCookie()
      .map((c) => c.split(';')[0])
      .join('; ');
    const demo = await fetch(`${base}/api/sources`, { method: 'POST', headers: { ...H, cookie }, body: JSON.stringify({ type: 'demo' }) });
    const st: any = await demo.json();
    expect(demo.status, JSON.stringify(st)).toBe(200);
    expect(st.deadlines).toHaveLength(5);
    expect(st.sessions.length).toBeGreaterThan(10);
    // Concurrent writes for one user are serialized by the advisory lock: none is lost.
    const results = await Promise.all(
      Array.from({ length: 5 }, (_, i) =>
        fetch(`${base}/api/blocks`, { method: 'POST', headers: { ...H, cookie }, body: JSON.stringify({ start: `2030-01-0${i + 1}`, reason: 'concurrent' }) }),
      ),
    );
    expect(results.every((r) => r.status === 200)).toBe(true);
    const after: any = await (await fetch(`${base}/api/state`, { headers: { cookie } })).json();
    expect(after.blocks.filter((b: any) => b.reason === 'concurrent')).toHaveLength(5);
    await db.execute(sql`update source set next_sync_at = now() - interval '1 minute'`);
    expect((await tick(db, 10_000)).synced).toBeGreaterThanOrEqual(1);
    const del = await fetch(`${base}/api/account`, { method: 'DELETE', headers: { ...H, cookie }, body: JSON.stringify({ confirm: 'DELETE' }) });
    expect(del.status).toBe(200);
    server.close();
    await close();
  }, 60_000);
});
