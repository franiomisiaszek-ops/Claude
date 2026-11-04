import fs from 'node:fs';
import path from 'node:path';
import type { DB } from '../shared/types';

export const DATA_DIR = process.env.DATA_DIR ?? path.resolve('data');
const DB_FILE = path.join(DATA_DIR, 'db.json');

const guessTimezone = () => Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';

export function defaultDB(): DB {
  return {
    version: 1,
    settings: {
      onboarded: false,
      timezone: guessTimezone(),
      digestTime: '07:30',
      slack: 0.85,
      minChunk: 25,
      maxSession: 60,
      maxSubjects: 3,
    },
    availability: { minutesByWeekday: [0, 120, 120, 120, 120, 90, 150] },
    blocks: [],
    courses: [],
    deadlines: [],
    materials: [],
    sessions: [],
    busy: [],
    sources: [],
    inbox: [],
    snapshots: [],
    digestLog: [],
    planMeta: { health: { status: 'ok', deadlines: [] } },
  };
}

let db: DB | null = null;

export function load(): DB {
  if (db) return db;
  fs.mkdirSync(DATA_DIR, { recursive: true });
  if (fs.existsSync(DB_FILE)) {
    const parsed = JSON.parse(fs.readFileSync(DB_FILE, 'utf8')) as DB;
    const d = defaultDB();
    db = { ...d, ...parsed, settings: { ...d.settings, ...parsed.settings }, planMeta: { ...d.planMeta, ...parsed.planMeta } };
  } else db = defaultDB();
  return db;
}

/** Atomic write: write a temp file, then rename over the old one. */
export function save(): void {
  if (!db) return;
  fs.mkdirSync(DATA_DIR, { recursive: true });
  const tmp = `${DB_FILE}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(db, null, 1), { mode: 0o600 });
  fs.renameSync(tmp, DB_FILE);
}

/** Replace the whole DB (used by tests and reset). */
export function replace(next: DB): void {
  db = next;
  save();
}

export function readSecretFile<T>(name: string): T | null {
  const f = path.join(DATA_DIR, name);
  return fs.existsSync(f) ? (JSON.parse(fs.readFileSync(f, 'utf8')) as T) : null;
}

export function writeSecretFile(name: string, value: unknown): void {
  fs.mkdirSync(DATA_DIR, { recursive: true });
  const f = path.join(DATA_DIR, name);
  fs.writeFileSync(f, JSON.stringify(value, null, 1), { mode: 0o600 });
  fs.chmodSync(f, 0o600);
}

export function deleteSecretFile(name: string): void {
  const f = path.join(DATA_DIR, name);
  if (fs.existsSync(f)) fs.unlinkSync(f);
}

export const uid = (prefix = '') => `${prefix}${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
