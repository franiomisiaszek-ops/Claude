/**
 * Effort estimation. Every number traces back to the amount of material:
 *
 *   coverage C = rate per unit × amount × "for me" difficulty × your pace
 *   item time  = C × "more / less time on this"
 *   exam total = Σ item time × "more / less time for this exam" × learned factor (from past grades)
 *
 * Pace is learned from logged session times; the learned factor from grades vs time studied.
 * Each factor has one meaning and one learning signal, so nothing is counted twice.
 */
import type { Course, Material, UnitType } from '../shared/types';

/** Minutes per unit of material, before difficulty and personal calibration. */
export const BASE_RATES: Record<UnitType, number> = {
  pages: 4,
  slides: 1.5,
  problems: 8,
  videoMinutes: 1.2,
  topics: 45,
  minutes: 1,
};

export const UNIT_LABEL: Record<UnitType, string> = {
  pages: 'pages',
  slides: 'slides',
  problems: 'problems',
  videoMinutes: 'video minutes',
  topics: 'topics',
  minutes: 'minutes',
};

/** "For me": easy 0.8, normal 1, hard 1.4. */
export const DIFFICULTY_FACTOR = { 1: 0.8, 2: 1, 3: 1.4 } as const;

/** Pace is applied after this many logged sessions. */
export const MIN_SAMPLES = 3;
const ALPHA = 0.3;

const clamp = (n: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, n));
const round5 = (n: number) => Math.max(5, Math.round(n / 5) * 5);

/** The pace factor currently applied to plans for a course (1 until enough samples). */
export function courseFactor(course?: Course): number {
  if (!course || course.samples < MIN_SAMPLES) return 1;
  return clamp(course.calibrationFactor, 0.5, 2);
}

/** Coverage minutes: one proper pass through the item at the user's pace. */
export function coverageMinutes(m: Material, course?: Course): number {
  if (m.estimateOverride != null && m.estimateOverride >= 0) return Math.round(m.estimateOverride);
  const raw = BASE_RATES[m.unitType] * m.quantity * DIFFICULTY_FACTOR[m.difficulty] * courseFactor(course);
  return round5(raw);
}

/** Item minutes including the user's one-off "more / less" choice. Overrides are taken as exact. */
export function estimateMinutes(m: Material, course?: Course): number {
  if (m.estimateOverride != null && m.estimateOverride >= 0) return Math.round(m.estimateOverride);
  return round5(coverageMinutes(m, course) * clamp(m.timeAdjust ?? 1, 0.25, 3));
}

export interface EstimateStep {
  label: string;
  factor?: number;
  minutes: number;
}

/** The chain shown to the user: "18 pages × 4 min = 72m → hard for me ×1.4 → your pace ×1.2 …". */
export function estimateSteps(m: Material, course?: Course): EstimateStep[] {
  if (m.estimateOverride != null && m.estimateOverride >= 0) return [{ label: 'Your exact time', minutes: Math.round(m.estimateOverride) }];
  const steps: EstimateStep[] = [];
  let v = BASE_RATES[m.unitType] * m.quantity;
  steps.push({ label: `${m.quantity} ${UNIT_LABEL[m.unitType]} × ${BASE_RATES[m.unitType]} min`, minutes: Math.round(v) });
  if (m.difficulty !== 2) {
    v *= DIFFICULTY_FACTOR[m.difficulty];
    steps.push({ label: m.difficulty === 1 ? 'Easy for you' : 'Hard for you', factor: DIFFICULTY_FACTOR[m.difficulty], minutes: Math.round(v) });
  }
  const pace = courseFactor(course);
  if (pace !== 1) {
    v *= pace;
    steps.push({ label: pace > 1 ? 'Your pace in this course (slower)' : 'Your pace in this course (faster)', factor: Math.round(pace * 100) / 100, minutes: Math.round(v) });
  }
  const adj = clamp(m.timeAdjust ?? 1, 0.25, 3);
  if (adj !== 1) {
    v = coverageMinutes(m, course) * adj;
    steps.push({ label: adj > 1 ? 'You asked for more time' : 'You asked for less time', factor: adj, minutes: round5(v) });
  }
  steps.push({ label: 'Planned', minutes: estimateMinutes(m, course) });
  return steps;
}

/**
 * Updates the pace factor from one session: r = actual ÷ (planned ÷ factor used when planning).
 * Averaging in log space converges to the true pace (averaging actual ÷ planned would settle at its square root).
 */
export function updateCalibration(course: Course, plannedMin: number, actualMin: number): Course {
  if (plannedMin <= 0 || actualMin <= 0) return course;
  const applied = courseFactor(course);
  const r = clamp((actualMin * applied) / plannedMin, 0.25, 4);
  const logF = course.samples === 0 ? Math.log(r) : (1 - ALPHA) * Math.log(course.calibrationFactor) + ALPHA * Math.log(r);
  return { ...course, calibrationFactor: Math.round(Math.exp(logF) * 1000) / 1000, samples: course.samples + 1 };
}
