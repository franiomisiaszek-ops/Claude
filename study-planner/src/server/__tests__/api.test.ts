import type { Server } from 'node:http';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { sql } from 'drizzle-orm';
import { createApp } from '../app';
import { createAuth } from '../auth';
import { connect, type DB } from '../db';
import { tick } from '../jobs';
import { ensureProfile } from '../repo';

let server: Server;
let base = '';
let db: DB;
let close: () => Promise<void>;

beforeAll(async () => {
  ({ db, close } = await connect({ dataDir: ':memory:' }));
  const auth = createAuth(db, { onUserCreated: (id) => ensureProfile(db, id) });
  server = createApp({ db, auth }).listen(0);
  base = `http://localhost:${(server.address() as any).port}`;
}, 60_000);

afterAll(async () => {
  server.close();
  await close();
});

const ORIGIN = 'http://localhost:3000';

async function signUp(email: string): Promise<string> {
  const res = await fetch(`${base}/api/auth/sign-up/email`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Origin: ORIGIN },
    body: JSON.stringify({ email, password: 'a long enough password', name: email.split('@')[0] }),
  });
  expect(res.status, await res.clone().text()).toBe(200);
  return res.headers
    .getSetCookie()
    .map((c) => c.split(';')[0])
    .join('; ');
}

const client = (cookie: string) => async (method: string, path: string, body?: unknown) => {
  const res = await fetch(`${base}${path}`, {
    method,
    headers: { cookie, Origin: ORIGIN, ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}) },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const json: any = await res.json().catch(() => ({}));
  return { status: res.status, json };
};

describe('API', () => {
  let a: ReturnType<typeof client>;
  let b: ReturnType<typeof client>;

  it('requires sign-in', async () => {
    const res = await fetch(`${base}/api/state`);
    expect(res.status).toBe(401);
  });

  it('creates a profile on sign-up and onboards', async () => {
    a = client(await signUp('ann@example.com'));
    const st = await a('GET', '/api/state');
    expect(st.status).toBe(200);
    expect(st.json.user.email).toBe('ann@example.com');
    expect(st.json.settings.onboarded).toBe(false);
    const done = await a('POST', '/api/onboarding', { timezone: 'Europe/Warsaw' });
    expect(done.json.settings.onboarded).toBe(true);
    expect(done.json.settings.timezone).toBe('Europe/Warsaw');
  });

  it('imports demo data and never invents time for unsized deadlines', async () => {
    const st = await a('POST', '/api/sources', { type: 'demo' });
    expect(st.status, JSON.stringify(st.json)).toBe(200);
    expect(st.json.deadlines.length).toBe(5);
    expect(st.json.sessions.length).toBeGreaterThan(10);
    expect(st.json.inbox.map((e: any) => e.title)).toEqual(['Week 7 lab']);
    const hw = st.json.deadlines.find((d: any) => d.title === 'Calculus homework 4');
    const health = st.json.planMeta.health.deadlines.find((h: any) => h.deadlineId === hw.id);
    expect(health.needsMaterial).toBe(true);
    expect(st.json.sessions.some((s: any) => s.deadlineId === hw.id)).toBe(false);
    // Choosing a rough time plans it.
    const sized = await a('PUT', `/api/deadlines/${hw.id}`, { roughMinutes: 90 });
    const minutes = sized.json.sessions.filter((s: any) => s.materialId === `auto:${hw.id}`).reduce((x: number, s: any) => x + s.minutes, 0);
    expect(minutes).toBe(90);
    expect(sized.json.canUndo).toBe('Edited a deadline');
  });

  it('keeps users apart', async () => {
    b = client(await signUp('bob@example.com'));
    const st = await b('GET', '/api/state');
    expect(st.json.deadlines).toHaveLength(0);
    const aState = await a('GET', '/api/state');
    const foreign = aState.json.deadlines[0].id;
    const res = await b('PUT', `/api/deadlines/${foreign}`, { title: 'hacked' });
    expect(res.status).toBe(400);
    const again = await a('GET', '/api/state');
    expect(again.json.deadlines.find((d: any) => d.id === foreign).title).not.toBe('hacked');
  });

  it('blocks cross-site writes', async () => {
    const res = await fetch(`${base}/api/onboarding`, { method: 'POST', headers: { Origin: 'https://evil.example', 'Content-Type': 'application/json' }, body: '{}' });
    expect(res.status).toBe(403);
  });

  it('records an exam result and learns from it', async () => {
    const st = await a('GET', '/api/state');
    const bio = st.json.deadlines.find((d: any) => d.title === 'Biology midterm');
    const material = st.json.materials.find((m: any) => m.deadlineId === bio.id);
    const r = await a('POST', '/api/results', {
      deadlineId: bio.id,
      minutesOutside: 300,
      feltDifficulty: 0,
      topicRatings: { [material.id]: 'weak' },
      grade: { scale: 'percent', value: 60 },
    });
    expect(r.status, JSON.stringify(r.json)).toBe(200);
    expect(r.json.results).toHaveLength(1);
    expect(r.json.results[0].score).toBeCloseTo(0.6);
    expect(r.json.results[0].baseEstimateMin).toBeGreaterThan(0);
    expect(r.json.materials.find((m: any) => m.id === material.id).weak).toBe(true);
    expect(r.json.learning[bio.courseId].factor).toBeGreaterThan(1);
    expect(r.json.learning[bio.courseId].explanation).toMatch(/60%/);
    const bad = await a('POST', '/api/results', { deadlineId: bio.id, grade: { scale: 'percent', value: 140 } });
    expect(bad.status).toBe(400);
  });

  it('calibrates pace from logged session time', async () => {
    const st = await a('GET', '/api/state');
    const s = st.json.sessions.find((x: any) => x.status === 'planned' && x.kind === 'new' && !x.materialId.includes(':'));
    const done = await a('PUT', `/api/sessions/${s.id}`, { status: 'done', actualMinutes: s.minutes * 2 });
    expect(done.json.courses.find((c: any) => c.id === s.courseId).samples).toBe(1);
  });

  it('undoes the last change', async () => {
    const st = await a('GET', '/api/state');
    const before = st.json.blocks.length;
    const blocked = await a('POST', '/api/blocks', { start: st.json.today, reason: 'test' });
    expect(blocked.json.blocks.length).toBe(before + 1);
    const undone = await a('POST', '/api/undo');
    expect(undone.json.blocks.length).toBe(before);
  });

  it('runs due background syncs', async () => {
    await db.execute(sql`update source set next_sync_at = now() - interval '1 minute'`);
    const out = await tick(db, 10_000);
    expect(out.synced).toBeGreaterThanOrEqual(1);
  });

  it('deletes an account and everything in it', async () => {
    const res = await b('DELETE', '/api/account', { confirm: 'DELETE' });
    expect(res.status).toBe(200);
    expect((await b('GET', '/api/state')).status).toBe(401);
    const rows: any = await db.execute(sql`select count(*)::int as n from profile`);
    expect((rows.rows ?? rows)[0].n).toBe(1);
  });
});
