/** Post-exam debrief helpers (pure). */
import { coverageMinutes } from '../planner/estimate';
import { targetFor } from '../planner/learning';
import type { TopicRating, UserState } from '../shared/types';

/**
 * Effort numbers for an exam, frozen when the debrief is saved:
 * - minutesInApp: time logged in done/partial sessions for this exam
 * - baseEstimateMin: material coverage (pages × rate × "for me" × pace), without one-off adjustments or learning
 * Items marked "not on the exam" are left out of both.
 */
export function effortFor(state: UserState, deadlineId: string, ratings: Record<string, TopicRating> = {}) {
  const notOnExam = new Set(Object.entries(ratings).filter(([, r]) => r === 'not_on_exam').map(([id]) => id));
  const sessions = state.sessions.filter((s) => s.deadlineId === deadlineId && (s.status === 'done' || s.status === 'partial'));
  const minutesInApp = sessions
    .filter((s) => !notOnExam.has(s.materialId))
    .reduce((a, s) => a + (s.actualMinutes ?? (s.status === 'partial' ? Math.round(s.minutes / 2) : s.minutes)), 0);
  const courseOf = (id: string) => state.courses.find((c) => c.id === id);
  const materials = state.materials.filter((m) => m.deadlineId === deadlineId && !notOnExam.has(m.id));
  let baseEstimateMin = materials.reduce((a, m) => a + coverageMinutes(m, courseOf(m.courseId)), 0);
  const d = state.deadlines.find((x) => x.id === deadlineId);
  if (!baseEstimateMin && d?.roughMinutes) baseEstimateMin = d.roughMinutes;
  return { minutesInApp, baseEstimateMin };
}

export const targetShare = (state: UserState, courseId: string) =>
  targetFor(
    state.courses.find((c) => c.id === courseId),
    state.settings,
  );
