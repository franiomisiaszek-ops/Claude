/**
 * Loads one user's whole state and saves it back as a minimal diff.
 * The domain code (planner, sync merge, routes) works on plain objects; this module maps them to rows.
 */
import { and, eq, getTableColumns, inArray, sql } from 'drizzle-orm';
import type { PgTable } from 'drizzle-orm/pg-core';
import { hhmmToMin, localParts } from '../shared/dates';
import type { Availability, BusyEvent, PlanMeta, Settings, UserState } from '../shared/types';
import { hash } from '../planner/planner';
import type { DB } from './db';
import * as s from './db/schema';

export const defaultSettings = (timezone = 'UTC'): Settings => ({
  onboarded: false,
  timezone,
  digestTime: '07:30',
  slack: 0.85,
  minChunk: 25,
  maxSession: 60,
  maxSubjects: 3,
  gradeScale: 'percent',
  targetGrade: { scale: 'percent', value: 80 },
  reminders: { push: true, email: false },
});

export const defaultAvailability = (): Availability => ({ minutesByWeekday: [0, 120, 120, 120, 120, 90, 150] });
const defaultPlanMeta = (): PlanMeta => ({ health: { status: 'ok', deadlines: [] } });

interface Collection {
  key: keyof UserState;
  table: PgTable;
  id: (x: any) => string;
  /** Upsert conflict target (JS column keys). */
  target: string[];
  /** Columns owned by background jobs, never overwritten by a state save. */
  skipUpdate?: string[];
  toDomain?: (x: any) => any;
  toStored?: (x: any) => any;
}

const busyId = (b: BusyEvent) => `b${hash(`${b.sourceId}|${b.date}|${b.startMin}|${b.endMin}|${b.title ?? ''}`)}`;

const COLLECTIONS: Collection[] = [
  { key: 'courses', table: s.course, id: (x) => x.id, target: ['id'] },
  { key: 'deadlines', table: s.deadline, id: (x) => x.id, target: ['id'] },
  { key: 'materials', table: s.material, id: (x) => x.id, target: ['id'] },
  { key: 'sessions', table: s.studySession, id: (x) => x.id, target: ['userId', 'id'] },
  { key: 'blocks', table: s.block, id: (x) => x.id, target: ['id'] },
  {
    key: 'busy',
    table: s.busyEvent,
    id: busyId,
    target: ['id'],
    toStored: (b) => ({ ...b, id: busyId(b) }),
    toDomain: ({ id: _id, ...b }) => b,
  },
  { key: 'sources', table: s.source, id: (x) => x.id, target: ['id'], skipUpdate: ['nextSyncAt', 'lockedUntil'] },
  { key: 'inbox', table: s.inboxEntry, id: (x) => x.id, target: ['id'] },
  { key: 'snapshots', table: s.snapshot, id: (x) => x.id, target: ['id'] },
  { key: 'digestLog', table: s.digestLog, id: (x) => `${x.date}|${x.channel}`, target: ['userId', 'date', 'channel'] },
  { key: 'results', table: s.examResult, id: (x) => x.id, target: ['id'] },
];

const JOB_COLUMNS = new Set(['nextSyncAt', 'lockedUntil']);

function toRow(table: PgTable, obj: Record<string, unknown>, userId: string): Record<string, unknown> {
  const cols = getTableColumns(table);
  const row: Record<string, unknown> = { userId };
  for (const [k, col] of Object.entries(cols)) {
    if (k === 'userId' || !(k in obj)) continue;
    let v = obj[k];
    if (v === undefined) v = null;
    if (v != null && col.columnType === 'PgTimestamp' && typeof v === 'string') v = new Date(v);
    row[k] = v;
  }
  return row;
}

function fromRow(row: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(row)) {
    if (k === 'userId' || JOB_COLUMNS.has(k) || v === null) continue;
    out[k] = v instanceof Date ? v.toISOString() : v;
  }
  return out;
}

/** Creates the profile row for a new user (idempotent). */
export async function ensureProfile(db: DB, userId: string, timezone = 'UTC'): Promise<void> {
  const settings = defaultSettings(timezone);
  await db
    .insert(s.profile)
    .values({ userId, settings, availability: defaultAvailability(), planMeta: defaultPlanMeta(), nextDigestAt: nextDigestAt(settings) })
    .onConflictDoNothing();
}

export async function loadState(db: DB, userId: string): Promise<UserState> {
  let [p] = await db.select().from(s.profile).where(eq(s.profile.userId, userId));
  if (!p) {
    await ensureProfile(db, userId);
    [p] = await db.select().from(s.profile).where(eq(s.profile.userId, userId));
  }
  const state = {
    settings: { ...defaultSettings(), ...(p.settings as Settings) },
    availability: p.availability as Availability,
    planMeta: { ...defaultPlanMeta(), ...(p.planMeta as PlanMeta) },
  } as UserState;
  await Promise.all(
    COLLECTIONS.map(async (c) => {
      const rows = await db
        .select()
        .from(c.table as any)
        .where(eq((c.table as any).userId, userId));
      const items = rows.map((r: Record<string, unknown>) => fromRow(r)).map((x: any) => (c.toDomain ? c.toDomain(x) : x));
      (state as any)[c.key] = items;
    }),
  );
  state.sessions.sort((a, b) => a.date.localeCompare(b.date) || a.id.localeCompare(b.id));
  state.snapshots.sort((a, b) => a.createdAt.localeCompare(b.createdAt));
  state.materials.sort((a, b) => a.order - b.order || a.id.localeCompare(b.id));
  state.inbox.sort((a, b) => a.createdAt.localeCompare(b.createdAt));
  return state;
}

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

/** Writes only what changed between `before` and `after`. Call inside `withUserLock`. */
export async function saveState(db: DB, userId: string, before: UserState, after: UserState): Promise<void> {
  const profileChanged = !same(before.settings, after.settings) || !same(before.availability, after.availability) || !same(before.planMeta, after.planMeta);
  if (profileChanged) {
    const digestChanged = before.settings.digestTime !== after.settings.digestTime || before.settings.timezone !== after.settings.timezone;
    await db
      .update(s.profile)
      .set({
        settings: after.settings,
        availability: after.availability,
        planMeta: after.planMeta,
        updatedAt: new Date(),
        ...(digestChanged ? { nextDigestAt: nextDigestAt(after.settings) } : {}),
      })
      .where(eq(s.profile.userId, userId));
  }
  for (const c of COLLECTIONS) {
    const prev = new Map(((before as any)[c.key] as any[]).map((x) => [c.id(x), x]));
    const next = new Map(((after as any)[c.key] as any[]).map((x) => [c.id(x), x]));
    const removed = [...prev.keys()].filter((k) => !next.has(k));
    const upserts = [...next.entries()].filter(([k, x]) => !prev.has(k) || !same(prev.get(k), x)).map(([, x]) => x);
    if (removed.length) {
      const t = c.table as any;
      if (c.key === 'digestLog') {
        for (const k of removed) {
          const [date, channel] = k.split('|');
          await db.delete(t).where(and(eq(t.userId, userId), eq(t.date, date), eq(t.channel, channel)));
        }
      } else {
        for (let i = 0; i < removed.length; i += 500) {
          await db.delete(t).where(and(eq(t.userId, userId), inArray(t.id, removed.slice(i, i + 500))));
        }
      }
    }
    if (upserts.length) {
      const rows = upserts.map((x) => toRow(c.table, c.toStored ? c.toStored(x) : x, userId));
      const cols = getTableColumns(c.table);
      const set: Record<string, unknown> = {};
      for (const [k, col] of Object.entries(cols)) {
        if (c.target.includes(k) || k === 'userId' || c.skipUpdate?.includes(k)) continue;
        set[k] = sql.raw(`excluded."${col.name}"`);
      }
      const target = c.target.map((k) => (cols as any)[k]);
      for (let i = 0; i < rows.length; i += 200) {
        await db
          .insert(c.table)
          .values(rows.slice(i, i + 200) as any)
          .onConflictDoUpdate({ target, set });
      }
    }
  }
}

/** The next UTC instant at which the user's local clock shows `digestTime`. */
export function nextDigestAt(settings: Pick<Settings, 'digestTime' | 'timezone'>, now: Date = new Date()): Date {
  const local = localParts(settings.timezone, now);
  const target = hhmmToMin(settings.digestTime);
  for (let addDay = 0; addDay < 3; addDay++) {
    const date = new Date(Date.parse(`${local.date}T00:00:00Z`) + addDay * 86_400_000).toISOString().slice(0, 10);
    const when = zonedToUtc(date, target, settings.timezone);
    if (when.getTime() > now.getTime() + 30_000) return when;
  }
  return new Date(now.getTime() + 86_400_000);
}

/** Converts a local date + minutes-from-midnight in `tz` to a UTC Date (DST-safe). */
export function zonedToUtc(date: string, minutes: number, tz: string): Date {
  const guess = Date.parse(`${date}T00:00:00Z`) + minutes * 60_000;
  const offsetAt = (t: number) => {
    const p = localParts(tz, new Date(t));
    const asUtc = Date.parse(`${p.date}T00:00:00Z`) + p.minutes * 60_000;
    return asUtc - Math.floor(t / 60_000) * 60_000;
  };
  let t = guess - offsetAt(guess);
  t = guess - offsetAt(t);
  return new Date(t);
}
