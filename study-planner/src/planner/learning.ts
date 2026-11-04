/**
 * Learning from grades: how much study time (relative to the material estimate) a student needs
 * in a course to reach their target. Pure and deterministic: recomputed from the stored results.
 *
 * Model: an exponential learning curve S = 1 − exp(−x / τ), where
 *   S = grade as a share of points (0–1),
 *   x = minutes actually studied ÷ material-based coverage estimate,
 *   τ = effort scale for this student in this course.
 * Each exam gives θ = ln τ = ln x − ln(−ln(1 − S)) − ln δ (δ adjusts for an exam that felt easier/harder).
 * Exams are combined in log space with weights for recency, reliability and information
 * (grades near 0% or 100% say little about effort), and shrunk toward the student's other courses,
 * which are shrunk toward a prior that reproduces the material-based plan.
 * Time factor for target T: g = τ · (−ln(1 − T)).
 *
 * Guardrails: no change until the first graded exam; at most +30% per exam; decreases need two exams
 * in a row pointing down and are limited to −10% per exam; g ∈ [0.7, 3]; g < 1 never cuts coverage
 * (only extra review), so one great grade can't shrink the plan below one full pass.
 */
import { fmtMin } from '../shared/dates';
import { formatGrade, normalizeGrade, scaleById } from '../shared/grades';
import type { Course, ExamResult, Settings } from '../shared/types';

export const MODEL_VERSION = 1;

const S_MIN = 0.02;
const S_MAX = 0.98;
const T_MIN = 0.5;
const T_MAX = 0.95;
const PRIOR_USER = 3;
const PRIOR_COURSE = 2;
const RECENCY = 0.7;
const MAX_UP = 1.3;
const MAX_DOWN = 0.9;
const G_MIN = 0.7;
const G_MAX = 3;
const OUTLIER = 0.7;
const FELT_DELTA: Record<string, number> = { '-1': 0.9, '0': 1, '1': 1.15 };

const clamp = (n: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, n));

/** Fisher-style information of a grade about effort: 1 at 63%, ~0.39 at 90%, ~0.17 at 95%. */
export const information = (S: number) => {
  const v = ((1 - S) * -Math.log(1 - S)) / Math.exp(-1);
  return v * v;
};

export const thetaOf = (x: number, S: number, felt?: number) => Math.log(x) - Math.log(-Math.log(1 - S)) - Math.log(FELT_DELTA[String(felt ?? 0)] ?? 1);
export const factorFor = (theta: number, T: number) => Math.exp(theta) * -Math.log(1 - T);

export function targetFor(course: Pick<Course, 'targetGrade'> | undefined, settings: Pick<Settings, 'targetGrade'>): number {
  const g = course?.targetGrade ?? settings.targetGrade;
  if (!g) return 0.8;
  try {
    return clamp(normalizeGrade(g), T_MIN, T_MAX);
  } catch {
    return 0.8;
  }
}

export interface Observation {
  resultId: string;
  courseId: string;
  title: string;
  dueAt: string;
  x: number;
  S: number;
  theta: number;
  weight: number;
  outlier: boolean;
  minutes: number;
  base: number;
}

export interface LearnedCourse {
  /** Multiplier applied to this course's plans. */
  factor: number;
  /** What the latest evidence alone suggests (before the gradual-change limits). */
  suggested: number;
  /** Graded exams used. */
  n: number;
  /** Effective number of observations after weighting (confidence). */
  nEff: number;
  target: number;
  locked: boolean;
  /** One plain-language sentence for "why this much time". */
  explanation: string | null;
  observations: Observation[];
}

export function learnedFactors(results: ExamResult[], courses: Course[], settings: Pick<Settings, 'targetGrade'>): Record<string, LearnedCourse> {
  const defaultT = targetFor(undefined, settings);
  const thetaPop = -Math.log(-Math.log(1 - defaultT));
  const usable = results
    .filter((r) => r.score != null && !r.excluded && r.baseEstimateMin > 0 && r.minutesInApp + r.minutesOutside > 0)
    .sort((a, b) => a.dueAt.localeCompare(b.dueAt) || a.recordedAt.localeCompare(b.recordedAt) || a.id.localeCompare(b.id));

  const obs: Observation[] = usable.map((r) => {
    let S = r.score!;
    let scaleWeight = 1;
    if (r.grade && scaleById(r.grade.scale).id === 'passfail') {
      // A pass only says "above the pass mark": weak evidence. A fail is strong evidence.
      const pass = String(r.grade.value) === 'pass';
      S = pass ? 0.6 : 0.4;
      scaleWeight = pass ? 0.3 : 1;
    }
    S = clamp(S, S_MIN, S_MAX);
    const minutes = r.minutesInApp + r.minutesOutside;
    const x = clamp(minutes / r.baseEstimateMin, 0.05, 10);
    const reliability = 1 - 0.5 * (r.minutesOutside / minutes);
    return {
      resultId: r.id,
      courseId: r.courseId,
      title: r.title,
      dueAt: r.dueAt,
      x,
      S,
      theta: thetaOf(x, S, r.feltDifficulty),
      weight: reliability * information(S) * scaleWeight,
      outlier: false,
      minutes,
      base: r.baseEstimateMin,
    };
  });

  const byCourse = new Map<string, Observation[]>();
  for (const o of obs) byCourse.set(o.courseId, [...(byCourse.get(o.courseId) ?? []), o]);

  const pooled = (list: Observation[], prior: number, priorWeight: number, decayFrom?: number) => {
    let num = prior * priorWeight;
    let den = priorWeight;
    list.forEach((o, i) => {
      const w = o.weight * (decayFrom != null ? RECENCY ** (decayFrom - i) : 1);
      num += w * o.theta;
      den += w;
    });
    return { theta: num / den, weight: den - priorWeight };
  };

  // Outliers: an exam far from what the rest of its course says counts for less (and is flagged).
  for (const list of byCourse.values()) {
    if (list.length < 2) continue;
    for (const o of list) {
      const rest = list.filter((x) => x !== o);
      const est = pooled(rest, thetaPop, PRIOR_COURSE).theta;
      const d = Math.abs(o.theta - est);
      if (d > OUTLIER) {
        o.weight *= OUTLIER / d;
        o.outlier = true;
      }
    }
  }

  const thetaUser = pooled(obs, thetaPop, PRIOR_USER).theta;
  const out: Record<string, LearnedCourse> = {};
  for (const course of courses) {
    const T = targetFor(course, settings);
    const list = byCourse.get(course.id) ?? [];
    const base: LearnedCourse = { factor: 1, suggested: 1, n: list.length, nEff: 0, target: T, locked: !!course.learningLocked, explanation: null, observations: list };
    if (course.learningLocked || !obs.length) {
      out[course.id] = base;
      continue;
    }
    if (!list.length) {
      // No grades in this course yet: borrow gently from the student's other courses.
      const raw = factorFor(thetaUser, T);
      const factor = clamp(raw, MAX_DOWN, MAX_UP);
      out[course.id] = {
        ...base,
        factor: Math.round(factor * 100) / 100,
        suggested: Math.round(clamp(raw, G_MIN, G_MAX) * 100) / 100,
        explanation: Math.abs(factor - 1) >= 0.05 ? `Based on your grades in other courses, plans here use ×${factor.toFixed(2)} the material time.` : null,
      };
      continue;
    }
    let g = 1;
    let gPrev = 1;
    let rawPrev: number | null = null;
    let raw = 1;
    for (let k = 0; k < list.length; k++) {
      const theta = pooled(list.slice(0, k + 1), thetaUser, PRIOR_COURSE, k).theta;
      raw = clamp(factorFor(theta, T), G_MIN, G_MAX);
      const before = g;
      if (raw >= g) g = Math.min(raw, g * MAX_UP);
      else if (rawPrev != null && rawPrev < gPrev) g = Math.max(raw, g * MAX_DOWN);
      gPrev = before;
      rawPrev = raw;
    }
    const nEff = list.reduce((a, o, i) => a + o.weight * RECENCY ** (list.length - 1 - i), 0);
    const last = usable.filter((r) => r.courseId === course.id).at(-1)!;
    const lastObs = list.at(-1)!;
    const factor = Math.round(clamp(g, G_MIN, G_MAX) * 100) / 100;
    const gradeText = last.grade ? formatGrade(last.grade) : `${Math.round(lastObs.S * 100)}%`;
    const pct = Math.round(lastObs.x * 100);
    let explanation = `Last time (${last.title}) you studied ${fmtMin(lastObs.minutes)}, ${pct}% of the material estimate, and got ${gradeText}.`;
    if (Math.abs(factor - 1) < 0.05) explanation += ' That matches your target, so plans stay material-based.';
    else if (factor > 1) explanation += ` To reach your target, plans for this course use ×${factor.toFixed(2)} the material time${factor < Math.round(raw * 100) / 100 ? ' (raised gradually, at most 30% per exam)' : ''}.`;
    else explanation += ` You've beaten your target with less time, so plans include less extra review (×${factor.toFixed(2)}). Covering the material is never cut.`;
    out[course.id] = { ...base, factor, suggested: Math.round(raw * 100) / 100, nEff: Math.round(nEff * 100) / 100, explanation };
  }
  return out;
}
