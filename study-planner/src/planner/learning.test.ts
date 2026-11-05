import { describe, expect, it } from 'vitest';
import { normalizeGrade, gradeForShare } from '../shared/grades';
import type { Course, ExamResult } from '../shared/types';
import { information, learnedFactors } from './learning';
import { updateCalibration, courseFactor } from './estimate';

const course = (id: string, extra: Partial<Course> = {}): Course => ({ id, name: id, color: '#888', calibrationFactor: 1, samples: 0, ...extra });
const settings = { targetGrade: { scale: 'percent', value: 85 } };
let n = 0;
const result = (courseId: string, minutes: number, base: number, score: number | undefined, extra: Partial<ExamResult> = {}): ExamResult => ({
  id: `r${++n}`,
  deadlineId: `d${n}`,
  courseId,
  title: `Exam ${n}`,
  kind: 'exam',
  dueAt: `2026-0${Math.min(9, n)}-15`,
  grade: score == null ? undefined : { scale: 'percent', value: Math.round(score * 100) },
  score,
  target: 0.85,
  minutesInApp: minutes,
  minutesOutside: 0,
  baseEstimateMin: base,
  baseSource: 'material',
  topicRatings: {},
  excluded: false,
  reminderCount: 0,
  recordedAt: `2026-0${Math.min(9, n)}-16T10:00:00.000Z`,
  ...extra,
});

describe('learning from grades', () => {
  it('changes nothing before the first graded exam', () => {
    const out = learnedFactors([result('bio', 240, 240, undefined)], [course('bio')], settings);
    expect(out.bio.factor).toBe(1);
    expect(out.bio.explanation).toBeNull();
  });

  it('raises time after a grade below target, at most 30% per exam, and explains it', () => {
    const out = learnedFactors([result('bio', 240, 240, 0.72)], [course('bio')], settings);
    expect(out.bio.factor).toBeGreaterThan(1.15);
    expect(out.bio.factor).toBeLessThanOrEqual(1.3);
    expect(out.bio.suggested).toBeGreaterThanOrEqual(out.bio.factor);
    expect(out.bio.explanation).toMatch(/4h.*100% of the material estimate.*72%/);
  });

  it('does not cut time after one great grade; needs two in a row, and then only 10%', () => {
    const one = learnedFactors([result('bio', 240, 240, 0.97)], [course('bio')], settings);
    expect(one.bio.factor).toBe(1);
    const two = learnedFactors([result('bio', 240, 240, 0.97), result('bio', 200, 240, 0.97)], [course('bio')], settings);
    expect(two.bio.factor).toBeLessThan(1);
    expect(two.bio.factor).toBeGreaterThanOrEqual(0.9);
  });

  it('ignores excluded exams and respects a locked course', () => {
    const r = [result('bio', 240, 240, 0.5, { excluded: true })];
    expect(learnedFactors(r, [course('bio')], settings).bio.factor).toBe(1);
    expect(learnedFactors([result('bio', 240, 240, 0.5)], [course('bio', { learningLocked: true })], settings).bio.factor).toBe(1);
  });

  it('borrows gently from other courses for a course with no grades', () => {
    const out = learnedFactors([result('bio', 240, 240, 0.6), result('bio', 300, 240, 0.65)], [course('bio'), course('chem')], settings);
    expect(out.chem.factor).toBeGreaterThan(1);
    expect(out.chem.factor).toBeLessThanOrEqual(1.3);
    expect(out.chem.factor).toBeLessThan(out.bio.factor);
  });

  it('treats a pass as weak evidence and a fail as strong evidence', () => {
    const pass = learnedFactors([result('bio', 240, 240, 0.6, { grade: { scale: 'passfail', value: 'pass' } })], [course('bio')], settings);
    const fail = learnedFactors([result('bio', 240, 240, 0.4, { grade: { scale: 'passfail', value: 'fail' } })], [course('bio')], settings);
    expect(fail.bio.factor).toBeGreaterThan(pass.bio.factor);
  });

  it('counts very high and very low grades as less informative', () => {
    expect(information(1 - Math.exp(-1))).toBeCloseTo(1, 5);
    expect(information(0.9)).toBeCloseTo(0.39, 2);
    expect(information(0.95)).toBeLessThan(0.2);
  });

  it('never raises time for a pass or a top grade bin', () => {
    const pass = learnedFactors([result('bio', 240, 240, 0.75, { grade: { scale: 'passfail', value: 'pass' } })], [course('bio')], settings);
    expect(pass.bio.factor).toBeLessThanOrEqual(1);
    const first = learnedFactors([result('bio', 240, 240, 0.78, { grade: { scale: 'uk', value: 'first' } })], [course('bio')], settings);
    expect(first.bio.factor).toBeLessThanOrEqual(1);
    // A bin below the target still raises it.
    const third = learnedFactors([result('bio', 240, 240, 0.45, { grade: { scale: 'uk', value: 'third' } })], [course('bio')], settings);
    expect(third.bio.factor).toBeGreaterThan(1);
  });

  it('counts a guessed estimate less and never learns from a copy of past time', () => {
    const material = learnedFactors([result('bio', 240, 240, 0.6)], [course('bio')], settings);
    const guess = learnedFactors([result('bio', 240, 240, 0.6, { baseSource: 'guess' })], [course('bio')], settings);
    const copy = learnedFactors([result('bio', 240, 240, 0.6, { baseSource: 'history' })], [course('bio')], settings);
    expect(guess.bio.factor).toBeGreaterThan(1);
    expect(guess.bio.factor).toBeLessThan(material.bio.factor);
    expect(copy.bio.factor).toBe(1);
  });

  it('does not jump a high-target course just because another course got a grade', () => {
    const highTarget = course('chem', { targetGrade: { scale: 'percent', value: 95 } });
    const out = learnedFactors([result('bio', 240, 240, 0.85)], [course('bio'), highTarget], settings);
    expect(out.chem.factor).toBeCloseTo(1, 1);
  });

  it('is deterministic', () => {
    const r = [result('bio', 240, 240, 0.7), result('bio', 260, 240, 0.8)];
    expect(learnedFactors(r, [course('bio')], settings)).toEqual(learnedFactors(r, [course('bio')], settings));
  });
});

describe('pace calibration', () => {
  it('uses the pace that was applied when the session was planned', () => {
    // 3 samples at true pace 1.5 switch the factor on; a session planned before that (pace 1) must not skew it.
    let c = course('bio');
    for (let i = 0; i < 3; i++) c = updateCalibration(c, 40, 60, 1);
    const before = c.calibrationFactor;
    c = updateCalibration(c, 40, 60, 1);
    expect(Math.abs(c.calibrationFactor - 1.5)).toBeLessThanOrEqual(Math.abs(before - 1.5) + 1e-9);
    expect(c.calibrationFactor).toBeCloseTo(1.5, 2);
  });

  it('converges to the true pace (not its square root)', () => {
    let c = course('bio');
    const truePace = 1.5;
    for (let i = 0; i < 25; i++) {
      const planned = 40 * courseFactor(c);
      c = updateCalibration(c, planned, 40 * truePace);
    }
    expect(courseFactor(c)).toBeCloseTo(1.5, 1);
  });
});

describe('grade scales', () => {
  it('maps national scales to a share of points', () => {
    expect(normalizeGrade({ scale: 'percent', value: 72 })).toBeCloseTo(0.72);
    expect(normalizeGrade({ scale: 'points', value: 30, max: 40 })).toBeCloseTo(0.75);
    expect(normalizeGrade({ scale: 'letter', value: 'B+' })).toBeCloseTo(0.88);
    expect(normalizeGrade({ scale: '1-6-low', value: 1 })).toBeGreaterThan(normalizeGrade({ scale: '1-6-low', value: 4 }));
    expect(normalizeGrade({ scale: '2-5-high', value: '4,5' })).toBeCloseTo(0.85);
    expect(() => normalizeGrade({ scale: '1-10', value: 11 })).toThrow();
    expect(gradeForShare('letter', 0.86).value).toBe('B');
  });
});
