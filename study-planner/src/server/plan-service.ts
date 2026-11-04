import { addDays, localParts } from '../shared/dates';
import type { DB } from '../shared/types';
import { diffPlans, plan } from '../planner/planner';

export const todayFor = (db: DB) => localParts(db.settings.timezone).date;

/** Re-plans from today and records health plus a diff against the previous plan. */
export function replan(db: DB, cause?: string): void {
  const today = todayFor(db);
  const before = db.sessions;
  const result = plan({
    today,
    freezeToday: db.planMeta.lastPlanDate === today,
    settings: db.settings,
    availability: db.availability,
    blocks: db.blocks,
    busy: db.busy,
    courses: db.courses,
    deadlines: db.deadlines,
    materials: db.materials,
    sessions: db.sessions,
  });
  const cutoff = addDays(today, -120);
  db.sessions = result.sessions.filter((s) => s.date >= cutoff);
  const diff = diffPlans(before, result.sessions, today);
  db.planMeta = {
    lastPlanDate: today,
    health: result.health,
    diff: { ...diff, cause },
    plannedAt: new Date().toISOString(),
  };
}

/** Saves a restorable copy of user-editable state before a user change (keeps the last 2). */
export function snapshot(db: DB, label: string): void {
  const state = structuredClone({
    availability: db.availability,
    blocks: db.blocks,
    courses: db.courses,
    deadlines: db.deadlines,
    materials: db.materials,
    sessions: db.sessions,
  });
  db.snapshots = [...db.snapshots, { createdAt: new Date().toISOString(), label, state }].slice(-2);
}

export function undo(db: DB): boolean {
  const snap = db.snapshots.pop();
  if (!snap) return false;
  Object.assign(db, structuredClone(snap.state));
  replan(db);
  db.planMeta.diff = { added: 0, removed: 0, changed: 0, summary: `Undid: ${snap.label}` };
  return true;
}
