import { addDays, localParts } from '../shared/dates';
import type { UserState } from '../shared/types';
import { diffPlans, plan } from '../planner/planner';
import { learnedFactors } from '../planner/learning';
import { randomId } from './crypto';

export const todayFor = (state: Pick<UserState, 'settings'>) => localParts(state.settings.timezone).date;

/** Re-plans from today and records health plus a diff against the previous plan. */
export function replan(state: UserState, cause?: string): void {
  const today = todayFor(state);
  const before = state.sessions;
  const result = plan({
    today,
    freezeToday: state.planMeta.lastPlanDate === today,
    settings: state.settings,
    availability: state.availability,
    blocks: state.blocks,
    busy: state.busy,
    courses: state.courses,
    deadlines: state.deadlines,
    materials: state.materials,
    sessions: state.sessions,
    learning: learnedFactors(state.results, state.courses, state.settings),
  });
  const cutoff = addDays(today, -120);
  state.sessions = result.sessions.filter((s) => s.date >= cutoff);
  const diff = diffPlans(before, result.sessions, today);
  state.planMeta = {
    lastPlanDate: today,
    health: result.health,
    diff: { ...diff, cause },
    plannedAt: new Date().toISOString(),
  };
}

/** Saves a restorable copy of user-editable state before a user change (keeps the last 2). */
export function snapshot(state: UserState, label: string): void {
  const copy = structuredClone({
    availability: state.availability,
    blocks: state.blocks,
    courses: state.courses,
    deadlines: state.deadlines,
    materials: state.materials,
    sessions: state.sessions,
  });
  state.snapshots = [...state.snapshots, { id: randomId('snap_'), createdAt: new Date().toISOString(), label, state: copy }].slice(-2);
}

export function undo(state: UserState): boolean {
  const snap = state.snapshots.at(-1);
  if (!snap) return false;
  state.snapshots = state.snapshots.slice(0, -1);
  Object.assign(state, structuredClone(snap.state));
  replan(state);
  state.planMeta.diff = { added: 0, removed: 0, changed: 0, summary: `Undid: ${snap.label}` };
  return true;
}
