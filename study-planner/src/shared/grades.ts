/**
 * Grading scales from around the world, each mapped to an approximate share of points earned (0–1).
 * Mapping uses piecewise-linear anchors because most national scales are not linear in points
 * (e.g. German 4.0 is a bare pass at ~50%, not 40%). The learning model only compares a user's
 * results with their own targets on the same scale, so small mapping differences cancel out.
 */
import type { GradeValue } from './types';

export interface GradeOption {
  value: string;
  label: string;
  norm: number;
}

export interface GradeScale {
  id: string;
  label: string;
  /** Countries or systems that use it, for the picker. */
  hint?: string;
  kind: 'numeric' | 'options' | 'points';
  /** numeric: [value, share] anchors, sorted by value. */
  anchors?: [number, number][];
  min?: number;
  max?: number;
  step?: number;
  higherIsBetter?: boolean;
  options?: GradeOption[];
  /** Pass/fail carries less information, so it moves the learning less. */
  weight?: number;
}

export const GRADE_SCALES: GradeScale[] = [
  { id: 'percent', label: 'Percentage (0–100%)', kind: 'numeric', anchors: [[0, 0], [100, 1]], min: 0, max: 100, step: 1, higherIsBetter: true },
  { id: 'points', label: 'Points (score out of a maximum)', kind: 'points' },
  {
    id: 'letter',
    label: 'Letter grades (A+ to F)',
    hint: 'US, Canada, many international schools',
    kind: 'options',
    options: [
      ['A+', 0.98], ['A', 0.95], ['A-', 0.91], ['B+', 0.88], ['B', 0.85], ['B-', 0.81], ['C+', 0.78], ['C', 0.75],
      ['C-', 0.71], ['D+', 0.68], ['D', 0.65], ['D-', 0.61], ['F', 0.4],
    ].map(([v, n]) => ({ value: v as string, label: v as string, norm: n as number })),
  },
  { id: 'gpa4', label: 'GPA (0.0–4.0)', hint: 'US college', kind: 'numeric', anchors: [[0, 0.4], [1, 0.65], [2, 0.75], [3, 0.85], [4, 0.95]], min: 0, max: 4, step: 0.1, higherIsBetter: true },
  {
    id: 'uk',
    label: 'UK degree classes',
    hint: 'UK universities',
    kind: 'options',
    options: [
      { value: 'first', label: 'First (70%+)', norm: 0.78 },
      { value: '2:1', label: 'Upper second, 2:1 (60–69%)', norm: 0.65 },
      { value: '2:2', label: 'Lower second, 2:2 (50–59%)', norm: 0.55 },
      { value: 'third', label: 'Third (40–49%)', norm: 0.45 },
      { value: 'fail', label: 'Fail (under 40%)', norm: 0.3 },
    ],
  },
  { id: 'ib', label: 'IB (1–7)', hint: 'International Baccalaureate', kind: 'numeric', anchors: [[1, 0.1], [2, 0.25], [3, 0.4], [4, 0.52], [5, 0.63], [6, 0.75], [7, 0.9]], min: 1, max: 7, step: 1, higherIsBetter: true },
  { id: '1-10', label: '1–10 (10 is best)', hint: 'Netherlands, Spain, Italy, Greece, Latin America', kind: 'numeric', anchors: [[1, 0], [5.5, 0.55], [10, 1]], min: 1, max: 10, step: 0.1, higherIsBetter: true },
  { id: '0-20', label: '0–20 (20 is best)', hint: 'France, Portugal, Belgium', kind: 'numeric', anchors: [[0, 0], [10, 0.5], [20, 1]], min: 0, max: 20, step: 0.5, higherIsBetter: true },
  { id: '1-6-high', label: '1–6 (6 is best)', hint: 'Poland (school), Switzerland', kind: 'numeric', anchors: [[1, 0.2], [2, 0.45], [3, 0.6], [4, 0.75], [5, 0.9], [6, 1]], min: 1, max: 6, step: 0.5, higherIsBetter: true },
  { id: '2-5-high', label: '2–5 (5 is best)', hint: 'Poland (university), Russia, Ukraine', kind: 'numeric', anchors: [[2, 0.3], [3, 0.55], [3.5, 0.65], [4, 0.75], [4.5, 0.85], [5, 0.95]], min: 2, max: 5, step: 0.5, higherIsBetter: true },
  { id: '1-6-low', label: '1–6 (1 is best)', hint: 'Germany', kind: 'numeric', anchors: [[1, 0.96], [2, 0.86], [3, 0.74], [4, 0.58], [5, 0.4], [6, 0.15]], min: 1, max: 6, step: 0.1, higherIsBetter: false },
  { id: '1-5-low', label: '1–5 (1 is best)', hint: 'Czechia, Slovakia, Austria', kind: 'numeric', anchors: [[1, 0.95], [2, 0.82], [3, 0.62], [4, 0.4], [5, 0.15]], min: 1, max: 5, step: 0.5, higherIsBetter: false },
  {
    id: 'passfail',
    label: 'Pass / fail',
    kind: 'options',
    weight: 0.4,
    options: [
      { value: 'pass', label: 'Pass', norm: 0.75 },
      { value: 'fail', label: 'Fail', norm: 0.35 },
    ],
  },
];

export const scaleById = (id: string): GradeScale => GRADE_SCALES.find((s) => s.id === id) ?? GRADE_SCALES[0];

function interpolate(anchors: [number, number][], v: number): number {
  const sorted = [...anchors].sort((a, b) => a[0] - b[0]);
  if (v <= sorted[0][0]) return sorted[0][1];
  if (v >= sorted.at(-1)![0]) return sorted.at(-1)![1];
  for (let i = 1; i < sorted.length; i++) {
    const [x0, y0] = sorted[i - 1];
    const [x1, y1] = sorted[i];
    if (v <= x1) return y0 + ((v - x0) / (x1 - x0)) * (y1 - y0);
  }
  return sorted.at(-1)![1];
}

export class GradeError extends Error {}

/** Share of points (0–1) for a grade. Throws GradeError for values outside the scale. */
export function normalizeGrade(g: GradeValue): number {
  const scale = scaleById(g.scale);
  if (scale.kind === 'points') {
    const v = Number(g.value);
    const max = Number(g.max);
    if (!Number.isFinite(v) || !Number.isFinite(max) || max <= 0 || v < 0 || v > max) throw new GradeError('Enter a score between 0 and the maximum.');
    return v / max;
  }
  if (scale.kind === 'options') {
    const opt = scale.options!.find((o) => o.value.toLowerCase() === String(g.value).toLowerCase());
    if (!opt) throw new GradeError(`Choose one of: ${scale.options!.map((o) => o.value).join(', ')}`);
    return opt.norm;
  }
  const v = Number(String(g.value).replace(',', '.'));
  if (!Number.isFinite(v) || v < scale.min! || v > scale.max!) throw new GradeError(`Enter a grade from ${scale.min} to ${scale.max}.`);
  return interpolate(scale.anchors!, v);
}

export function formatGrade(g: GradeValue): string {
  const scale = scaleById(g.scale);
  if (scale.kind === 'points') return `${g.value}/${g.max}`;
  if (scale.kind === 'options') return scale.options!.find((o) => o.value === g.value)?.label ?? String(g.value);
  return scale.id === 'percent' ? `${g.value}%` : String(g.value);
}

/** Nearest grade on a scale for a share of points, e.g. to show "you'd likely get a B". */
export function gradeForShare(scaleId: string, share: number): GradeValue {
  const scale = scaleById(scaleId);
  if (scale.kind === 'points') return { scale: scaleId, value: Math.round(share * 100), max: 100 };
  if (scale.kind === 'options') {
    const best = [...scale.options!].sort((a, b) => Math.abs(a.norm - share) - Math.abs(b.norm - share))[0];
    return { scale: scaleId, value: best.value };
  }
  let bestV = scale.min!;
  let bestD = Infinity;
  for (let v = scale.min!; v <= scale.max! + 1e-9; v += scale.step ?? 1) {
    const d = Math.abs(interpolate(scale.anchors!, v) - share);
    if (d < bestD) {
      bestD = d;
      bestV = Math.round(v * 100) / 100;
    }
  }
  return { scale: scaleId, value: bestV };
}
