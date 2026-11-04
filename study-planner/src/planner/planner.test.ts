import { describe, expect, it } from 'vitest';
import { addDays, weekday } from '../shared/dates';
import type { Course, Deadline, Material, Session } from '../shared/types';
import { estimateMinutes, updateCalibration } from './estimate';
import { baseCapacity, diffPlans, plan, type PlanInput } from './planner';

const TODAY = '2026-11-02'; // a Monday

const course = (id: string, extra: Partial<Course> = {}): Course => ({ id, name: id, color: '#888', calibrationFactor: 1, samples: 0, ...extra });
const deadline = (id: string, courseId: string, dueIn: number, kind: Deadline['kind'] = 'exam'): Deadline => ({
  id,
  courseId,
  kind,
  title: `${id} ${kind}`,
  dueAt: addDays(TODAY, dueIn),
  status: 'active',
  userEdited: [],
  confidence: 1,
});
const material = (id: string, courseId: string, deadlineId: string | undefined, pages: number, order = 0): Material => ({
  id,
  courseId,
  deadlineId,
  title: id,
  unitType: 'pages',
  quantity: pages,
  difficulty: 2,
  order,
  done: false,
  userEdited: [],
});

const base = (over: Partial<PlanInput> = {}): PlanInput => ({
  today: TODAY,
  freezeToday: false,
  settings: { slack: 1, minChunk: 25, maxSession: 60, maxSubjects: 3 },
  availability: { minutesByWeekday: [120, 120, 120, 120, 120, 120, 120] },
  blocks: [],
  busy: [],
  courses: [course('bio'), course('chem'), course('math'), course('hist')],
  deadlines: [],
  materials: [],
  sessions: [],
  ...over,
});

const newMinutes = (ss: Session[], materialId?: string) =>
  ss.filter((s) => s.kind === 'new' && s.status === 'planned' && (!materialId || s.materialId === materialId)).reduce((a, s) => a + s.minutes, 0);

describe('estimator', () => {
  it('uses base rates, difficulty and overrides', () => {
    expect(estimateMinutes(material('a', 'bio', undefined, 10), course('bio'))).toBe(40);
    expect(estimateMinutes({ ...material('a', 'bio', undefined, 10), difficulty: 3 }, course('bio'))).toBe(55);
    expect(estimateMinutes({ ...material('a', 'bio', undefined, 10), estimateOverride: 17 }, course('bio'))).toBe(17);
  });
  it('applies calibration only after 3 samples, clamped', () => {
    let c = course('bio');
    c = updateCalibration(c, 30, 60);
    c = updateCalibration(c, 30, 60);
    expect(estimateMinutes(material('a', 'bio', undefined, 10), c)).toBe(40);
    c = updateCalibration(c, 30, 60);
    expect(c.samples).toBe(3);
    expect(estimateMinutes(material('a', 'bio', undefined, 10), c)).toBe(80);
    const extreme = { ...c, calibrationFactor: 9 };
    expect(estimateMinutes(material('a', 'bio', undefined, 10), extreme)).toBe(80);
  });
});

describe('capacity', () => {
  it('is zero on blocked days and scaled by slack', () => {
    const input = base({ settings: { ...base().settings, slack: 0.85 }, blocks: [{ id: 'b', start: TODAY, end: TODAY }] });
    expect(baseCapacity(TODAY, input)).toBe(0);
    expect(baseCapacity(addDays(TODAY, 1), input)).toBe(102);
  });
  it('subtracts only busy time inside the study window', () => {
    const input = base({
      availability: { minutesByWeekday: [180, 180, 180, 180, 180, 180, 180], studyWindow: { start: '17:00', end: '20:00' } },
      busy: [
        { sourceId: 'x', date: TODAY, startMin: 9 * 60, endMin: 10 * 60 }, // morning lecture: no effect
        { sourceId: 'x', date: TODAY, startMin: 18 * 60, endMin: 19 * 60 + 30 }, // eats 90 min of the window
      ],
    });
    expect(baseCapacity(TODAY, input)).toBe(90);
  });
});

describe('planner', () => {
  const single = () =>
    base({
      deadlines: [deadline('bio', 'bio', 10)],
      materials: [material('ch1', 'bio', 'bio', 10, 1), material('ch2', 'bio', 'bio', 10, 2), material('ch3', 'bio', 'bio', 10, 3), material('ch4', 'bio', 'bio', 10, 4)],
    });

  it('schedules all required minutes before the buffer and nothing on the due date', () => {
    const { sessions, health } = plan(single());
    expect(newMinutes(sessions)).toBe(160);
    const due = addDays(TODAY, 10);
    for (const s of sessions) {
      expect(s.date < due).toBe(true);
      if (s.kind === 'new') expect(s.date <= addDays(due, -2)).toBe(true);
    }
    expect(sessions.some((s) => s.date === addDays(due, -1) && s.reasons[0].code === 'bufferReview')).toBe(true);
    expect(health.status).toBe('ok');
    expect(health.deadlines[0].shortfallMin).toBe(0);
  });

  it('spreads work across the window instead of cramming', () => {
    const { sessions } = plan(single());
    const days = new Set(sessions.filter((s) => s.kind === 'new').map((s) => s.date));
    expect(days.size).toBeGreaterThanOrEqual(5);
    for (const s of sessions) expect(s.minutes).toBeLessThanOrEqual(60);
  });

  it('keeps syllabus order within a deadline', () => {
    const { sessions } = plan(single());
    const first = (id: string) => sessions.filter((s) => s.kind === 'new' && s.materialId === id).map((s) => s.date).sort()[0];
    expect(first('ch1') <= first('ch2')).toBe(true);
    expect(first('ch2') <= first('ch3')).toBe(true);
    expect(first('ch3') <= first('ch4')).toBe(true);
  });

  it('adds spaced reviews for exam material', () => {
    const { sessions } = plan(single());
    expect(sessions.some((s) => s.kind === 'review' && s.reasons[0].code === 'review')).toBe(true);
  });

  it('is deterministic', () => {
    expect(plan(single())).toEqual(plan(single()));
  });

  it('is stable when re-planned with its own output', () => {
    const first = plan(single());
    const again = plan({ ...single(), sessions: first.sessions, freezeToday: true });
    expect(diffPlans(first.sessions, again.sessions, TODAY).summary).toBe('No changes');
  });

  it('keeps existing sessions in place when an unrelated deadline is added', () => {
    const first = plan(single());
    const withEssay = { ...single(), deadlines: [...single().deadlines, deadline('essay', 'hist', 20, 'assignment')], sessions: first.sessions };
    const second = plan(withEssay);
    const bioBefore = first.sessions.filter((s) => s.kind === 'new').map((s) => `${s.date}|${s.materialId}|${s.minutes}`);
    const bioAfter = second.sessions.filter((s) => s.kind === 'new' && s.courseId === 'bio').map((s) => `${s.date}|${s.materialId}|${s.minutes}`);
    expect(bioAfter).toEqual(bioBefore);
  });

  it('moves sessions off a day that becomes blocked', () => {
    const first = plan(single());
    const day = first.sessions.find((s) => s.kind === 'new' && s.date > TODAY)!.date;
    const second = plan({ ...single(), sessions: first.sessions, blocks: [{ id: 'b', start: day, end: day }] });
    expect(second.sessions.some((s) => s.date === day)).toBe(false);
    expect(newMinutes(second.sessions)).toBe(160);
  });

  it('never schedules on blocked days', () => {
    const blocks = [{ id: 'b', start: addDays(TODAY, 2), end: addDays(TODAY, 4) }];
    const { sessions } = plan({ ...single(), blocks });
    expect(sessions.some((s) => s.date >= blocks[0].start && s.date <= blocks[0].end)).toBe(false);
    expect(newMinutes(sessions)).toBe(160);
  });

  it('rolls missed sessions over to future days', () => {
    const yesterday = addDays(TODAY, -1);
    const missed: Session = {
      id: 'old',
      date: yesterday,
      materialId: 'ch1',
      deadlineId: 'bio',
      courseId: 'bio',
      kind: 'new',
      minutes: 40,
      pinned: false,
      status: 'planned',
      reasons: [],
    };
    const { sessions } = plan({ ...single(), sessions: [missed] });
    expect(sessions.find((s) => s.id === 'old')!.status).toBe('missed');
    expect(newMinutes(sessions, 'ch1')).toBe(40);
    expect(sessions.find((s) => s.materialId === 'ch1' && s.date >= TODAY)!.reasons.some((r) => r.code === 'catchup')).toBe(true);
  });

  it('credits done and partial progress', () => {
    const yesterday = addDays(TODAY, -1);
    const done: Session[] = [
      { id: 'a', date: yesterday, materialId: 'ch1', deadlineId: 'bio', courseId: 'bio', kind: 'new', minutes: 40, pinned: false, status: 'done', reasons: [] },
      { id: 'b', date: yesterday, materialId: 'ch2', deadlineId: 'bio', courseId: 'bio', kind: 'new', minutes: 40, pinned: false, status: 'partial', actualMinutes: 15, reasons: [] },
    ];
    const { sessions, health } = plan({ ...single(), sessions: done });
    expect(newMinutes(sessions, 'ch1')).toBe(0);
    expect(newMinutes(sessions, 'ch2')).toBe(25);
    expect(health.deadlines[0].doneMin).toBe(55);
  });

  it('keeps pinned sessions and counts them toward the plan', () => {
    const pinned: Session = {
      id: 'p',
      date: addDays(TODAY, 3),
      materialId: 'ch1',
      deadlineId: 'bio',
      courseId: 'bio',
      kind: 'new',
      minutes: 40,
      pinned: true,
      status: 'planned',
      reasons: [],
    };
    const { sessions } = plan({ ...single(), sessions: [pinned] });
    const p = sessions.find((s) => s.id === 'p')!;
    expect(p.date).toBe(addDays(TODAY, 3));
    expect(p.reasons[0].code).toBe('pinned');
    expect(newMinutes(sessions, 'ch1')).toBe(40);
    expect(newMinutes(sessions)).toBe(160);
  });

  it('shrinks the buffer when the window is too short', () => {
    const input = base({
      availability: { minutesByWeekday: [60, 60, 60, 60, 60, 60, 60] },
      deadlines: [deadline('bio', 'bio', 3)],
      materials: [material('ch1', 'bio', 'bio', 35)], // 140 min: more than the 2 days before the buffer
    });
    const { health, sessions } = plan(input);
    expect(health.deadlines[0].bufferShrunk).toBe(true);
    expect(health.status).toBe('tight');
    expect(newMinutes(sessions)).toBe(140);
  });

  it('reports a shortfall with fixes when overloaded', () => {
    const input = base({
      availability: { minutesByWeekday: [0, 60, 60, 60, 60, 60, 0] },
      deadlines: [deadline('bio', 'bio', 4)],
      materials: [material('ch1', 'bio', 'bio', 100)], // 400 min
    });
    const { health } = plan(input);
    expect(health.status).toBe('atRisk');
    expect(health.deadlines[0].shortfallMin).toBeGreaterThan(0);
    expect(health.deadlines[0].fixes.length).toBeGreaterThan(0);
  });

  it('creates a default block of work for deadlines without material', () => {
    const { sessions } = plan(base({ deadlines: [deadline('essay', 'hist', 6, 'assignment')] }));
    expect(newMinutes(sessions, 'auto:essay')).toBe(90);
    expect(sessions.every((s) => s.kind === 'new')).toBe(true); // assignments get no reviews
  });

  it('prioritises the earlier deadline when time is tight', () => {
    const input = base({
      availability: { minutesByWeekday: [60, 60, 60, 60, 60, 60, 60] },
      deadlines: [deadline('late', 'chem', 6), deadline('soon', 'bio', 3)],
      materials: [material('s1', 'bio', 'soon', 30), material('l1', 'chem', 'late', 30)],
    });
    const { health } = plan(input);
    expect(health.deadlines.find((h) => h.deadlineId === 'soon')!.shortfallMin).toBe(0);
  });

  it('caps the number of subjects per day', () => {
    const input = base({
      settings: { slack: 1, minChunk: 25, maxSession: 60, maxSubjects: 2 },
      availability: { minutesByWeekday: [300, 300, 300, 300, 300, 300, 300] },
      deadlines: ['bio', 'chem', 'math', 'hist'].map((c) => deadline(c, c, 5)),
      materials: ['bio', 'chem', 'math', 'hist'].map((c) => material(`${c}1`, c, c, 15)),
    });
    const { sessions } = plan(input);
    const byDay = new Map<string, Set<string>>();
    for (const s of sessions) byDay.set(s.date, (byDay.get(s.date) ?? new Set()).add(s.courseId));
    for (const set of byDay.values()) expect(set.size).toBeLessThanOrEqual(2);
  });

  it('schedules self-paced material within the next weeks', () => {
    const { sessions } = plan(base({ materials: [material('notes', 'math', undefined, 20)] }));
    expect(newMinutes(sessions, 'notes')).toBe(80);
    expect(sessions.every((s) => s.reasons.some((r) => r.code === 'selfPaced'))).toBe(true);
  });

  it('respects days with no study time', () => {
    const avail = { minutesByWeekday: [0, 120, 120, 120, 120, 120, 0] };
    const { sessions } = plan({ ...single(), availability: avail });
    for (const s of sessions) expect([0, 6]).not.toContain(weekday(s.date));
  });
});

describe('diffPlans', () => {
  it('summarises additions, removals and resizes', () => {
    const s = (date: string, minutes: number, materialId = 'm'): Session => ({
      id: date + materialId,
      date,
      materialId,
      courseId: 'c',
      kind: 'new',
      minutes,
      pinned: false,
      status: 'planned',
      reasons: [],
    });
    const d = diffPlans([s(TODAY, 30), s(addDays(TODAY, 1), 30)], [s(TODAY, 45), s(addDays(TODAY, 2), 30)], TODAY);
    expect(d).toEqual({ added: 1, removed: 1, changed: 1, summary: '1 added, 1 removed, 1 resized' });
  });
});
