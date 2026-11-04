import type { Server } from 'node:http';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { sql } from 'drizzle-orm';
import { createApp } from '../app';
import { createAuth } from '../auth';
import { connect, type DB } from '../db';
import { tick } from '../jobs';
import { ensureProfile } from '../repo';
import { __APP_TABLES, assertTableOwnership } from '../setup';

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

  it('has row-level security on every table, owned by the app role', async () => {
    const res: any = await db.execute(sql`select relname, relrowsecurity from pg_class where relkind = 'r' and relnamespace = 'public'::regnamespace`);
    const rows: { relname: string; relrowsecurity: boolean }[] = res.rows ?? res;
    const app = rows.filter((r) => __APP_TABLES.includes(r.relname));
    expect(app.length).toBe(__APP_TABLES.length);
    expect(app.filter((r) => !r.relrowsecurity).map((r) => r.relname)).toEqual([]);
    await expect(assertTableOwnership(db)).resolves.toBeUndefined();
  });

  it('asks age neutrally: under 13 deletes the account; minors get no AI', async () => {
    const kid = client(await signUp('kid@example.com'));
    const y = new Date().getUTCFullYear() - 10;
    const del = await kid('POST', '/api/age', { birthMonth: `${y}-06` });
    expect(del.json.deleted).toBe(true);
    expect((await kid('GET', '/api/state')).status).toBe(401);
    const teenCookie = await signUp('teen@example.com');
    const teen = client(teenCookie);
    const t = await teen('POST', '/api/age', { birthMonth: `${new Date().getUTCFullYear() - 15}-01` });
    expect(t.status).toBe(200);
    expect(t.json.settings.birthMonth).toMatch(/^\d{4}-01$/);
    expect(t.json.features.ai).toBe(false);
    expect((await teen('POST', '/api/age', { birthMonth: '1990-01' })).status).toBe(400); // can't be changed later
    const syl = await fetch(`${base}/api/ai/syllabus?courseName=X`, { method: 'POST', headers: { cookie: teenCookie, Origin: ORIGIN, 'Content-Type': 'text/plain' }, body: 'x'.repeat(100) });
    expect(syl.status).toBe(403);
  });

  it('deletes an account and everything in it', async () => {
    const res = await b('DELETE', '/api/account', { confirm: 'DELETE' });
    expect(res.status).toBe(200);
    expect((await b('GET', '/api/state')).status).toBe(401);
    const rows: any = await db.execute(sql`select count(*)::int as n from profile p join "user" u on u.id = p.user_id where u.email = 'bob@example.com'`);
    expect((rows.rows ?? rows)[0].n).toBe(0);
  });
});
