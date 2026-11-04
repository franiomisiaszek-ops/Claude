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

export const DIFFICULTY_FACTOR = { 1: 0.8, 2: 1, 3: 1.4 } as const;

/** Calibration only kicks in after this many logged sessions. */
export const MIN_SAMPLES = 3;
const ALPHA = 0.3;

const clamp = (n: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, n));

export function courseFactor(course?: Course): number {
  if (!course || course.samples < MIN_SAMPLES) return 1;
  return clamp(course.calibrationFactor, 0.5, 2);
}

export function estimateMinutes(m: Material, course?: Course): number {
  if (m.estimateOverride != null && m.estimateOverride >= 0) return Math.round(m.estimateOverride);
  const raw = BASE_RATES[m.unitType] * m.quantity * DIFFICULTY_FACTOR[m.difficulty] * courseFactor(course);
  return Math.max(5, Math.round(raw / 5) * 5);
}

/** Exponential moving average of actual ÷ planned minutes, recorded per course. */
export function updateCalibration(course: Course, plannedMin: number, actualMin: number): Course {
  if (plannedMin <= 0 || actualMin <= 0) return course;
  const ratio = clamp(actualMin / plannedMin, 0.25, 4);
  const factor = course.samples === 0 ? ratio : course.calibrationFactor * (1 - ALPHA) + ratio * ALPHA;
  return { ...course, calibrationFactor: Math.round(factor * 1000) / 1000, samples: course.samples + 1 };
}
