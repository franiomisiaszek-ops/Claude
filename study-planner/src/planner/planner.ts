/**
 * The study planner: a pure, deterministic function of its inputs.
 * It does no I/O and never reads the clock; `today` is passed in.
 *
 * Steps:
 *  1. Reconcile the past: unmarked past sessions become `missed`, done/partial minutes count as progress.
 *  2. Keep fixed sessions: pinned ones, anything already checked in, and (once planned today) today's sessions.
 *  3. Estimate remaining minutes per material (deadlines with no material get a default block).
 *  4. Compute daily capacity: weekly template, minus busy time in the study window, minus blocks, × slack.
 *  5. Windows: new material must finish `buffer` days before the deadline.
 *  6. Earliest-deadline-first water-fill: spread each deadline's work evenly across its window.
 *  7. Triage: deadlines that don't fit lose their buffer and are re-planned; reviews only use leftover time.
 *  8. Spaced reviews (+1/+3/+7 days) and a buffer-day review for exams and quizzes.
 *  9. Health: per-deadline shortfall with concrete fixes.
 */
import type {
  Availability,
  Block,
  BusyEvent,
  Course,
  Deadline,
  DeadlineHealth,
  DeadlineKind,
  Health,
  ISODate,
  Material,
  PlanDiff,
  Reason,
  Session,
  Settings,
} from '../shared/types';
import { addDays, dateRange, diffDays, hhmmToMin, weekday, WEEKDAYS } from '../shared/dates';
import { estimateMinutes } from './estimate';

export interface PlanInput {
  today: ISODate;
  /** True once a plan has already been made today: today's planned sessions then stay put. */
  freezeToday: boolean;
  settings: Pick<Settings, 'slack' | 'minChunk' | 'maxSession' | 'maxSubjects'>;
  availability: Availability;
  blocks: Block[];
  busy: BusyEvent[];
  courses: Course[];
  deadlines: Deadline[];
  materials: Material[];
  sessions: Session[];
}

export interface PlanResult {
  sessions: Session[];
  health: Health;
}

export const BUFFER_DAYS: Record<DeadlineKind, number> = { exam: 2, quiz: 1, assignment: 1 };
export const DEFAULT_MINUTES: Record<DeadlineKind, number> = { exam: 180, quiz: 60, assignment: 90 };
export const SELF_PACED_DAYS = 28;
const REVIEW_OFFSETS = [1, 3, 7];
const REVIEW_SHARE = 0.2;

const round5 = (n: number) => Math.max(5, Math.round(n / 5) * 5);

/** Small stable string hash (FNV-1a) for deterministic session ids. */
export function hash(s: string): string {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return (h >>> 0).toString(36);
}

interface Unit {
  materialId: string;
  title: string;
  order: number;
  availableFrom: ISODate;
  total: number;
  credited: number;
  remaining: number;
  catchup: boolean;
}

interface Group {
  key: string;
  deadline?: Deadline;
  courseId: string;
  start: ISODate;
  /** Last day new material may be scheduled. */
  end: ISODate;
  buffer: number;
  units: Unit[];
  required: number;
}

interface Day {
  date: ISODate;
  /** Capacity before fixed sessions. */
  base: number;
  cap: number;
  used: number;
  courses: Set<string>;
}

export const isBlocked = (date: ISODate, blocks: Block[]) => blocks.some((b) => b.start <= date && date <= b.end);

/** Minutes of study capacity on a date, before fixed sessions are subtracted. */
export function baseCapacity(date: ISODate, input: Pick<PlanInput, 'availability' | 'blocks' | 'busy' | 'settings'>): number {
  if (isBlocked(date, input.blocks)) return 0;
  let cap = input.availability.minutesByWeekday[weekday(date)] ?? 0;
  const win = input.availability.studyWindow;
  if (win && cap > 0) {
    const ws = hhmmToMin(win.start);
    const we = hhmmToMin(win.end);
    const intervals = input.busy
      .filter((b) => b.date === date)
      .map((b) => [Math.max(ws, b.startMin), Math.min(we, b.endMin)] as const)
      .filter(([s, e]) => e > s)
      .sort((a, b) => a[0] - b[0]);
    let busyMin = 0;
    let curS = -1;
    let curE = -1;
    for (const [s, e] of intervals) {
      if (s > curE) {
        if (curE > curS) busyMin += curE - curS;
        curS = s;
        curE = e;
      } else curE = Math.max(curE, e);
    }
    if (curE > curS) busyMin += curE - curS;
    cap = Math.min(cap, Math.max(0, we - ws - busyMin));
  }
  return Math.floor(cap * input.settings.slack);
}

function credit(s: Session): number {
  if (s.kind !== 'new') return 0;
  if (s.status === 'done') return s.minutes;
  if (s.status === 'partial') return Math.min(s.minutes, s.actualMinutes ?? Math.round(s.minutes / 2));
  return 0;
}

const sessionLoad = (s: Session) => (s.status === 'done' || s.status === 'partial' ? s.actualMinutes ?? s.minutes : s.minutes);

export function plan(input: PlanInput): PlanResult {
  const { today, settings } = input;
  const courseById = new Map(input.courses.map((c) => [c.id, c]));
  const deadlineById = new Map(input.deadlines.map((d) => [d.id, d]));
  const todayBlocked = isBlocked(today, input.blocks);

  // 1–2. History and fixed sessions.
  const history: Session[] = [];
  const fixed: Session[] = [];
  for (const s of input.sessions) {
    if (s.date < today) history.push(s.status === 'planned' ? { ...s, status: 'missed' } : s);
    else if (s.status !== 'planned') fixed.push(s);
    else if (s.pinned) fixed.push(s);
    else if (s.date === today && input.freezeToday && !todayBlocked) fixed.push(s);
  }
  const kept = [...history, ...fixed];
  const keptIds = new Set(kept.map((s) => s.id));
  /** Previous auto-planned future sessions: kept where they still fit, to avoid reshuffling the calendar. */
  const previous = input.sessions
    .filter((s) => !keptIds.has(s.id) && s.date >= today && s.status === 'planned' && s.kind === 'new' && !(todayBlocked && s.date === today))
    .sort((a, b) => a.date.localeCompare(b.date) || a.id.localeCompare(b.id));

  const creditBy = new Map<string, number>();
  const fixedFutureBy = new Map<string, number>();
  const missedBy = new Set<string>();
  for (const s of kept) {
    creditBy.set(s.materialId, (creditBy.get(s.materialId) ?? 0) + credit(s));
    if (s.kind === 'new' && s.status === 'planned') fixedFutureBy.set(s.materialId, (fixedFutureBy.get(s.materialId) ?? 0) + s.minutes);
    if (s.kind === 'new' && (s.status === 'missed' || s.status === 'skipped')) missedBy.add(s.materialId);
  }

  // 3. Units of remaining work, grouped by deadline (or per course for self-paced material).
  const groups = new Map<string, Group>();
  const activeDeadlines = input.deadlines.filter((d) => d.status === 'active' && d.dueAt > today);
  for (const d of activeDeadlines) {
    groups.set(d.id, { key: d.id, deadline: d, courseId: d.courseId, start: today, end: today, buffer: BUFFER_DAYS[d.kind], units: [], required: 0 });
  }
  const materials = [...input.materials].sort((a, b) => a.order - b.order || a.title.localeCompare(b.title) || a.id.localeCompare(b.id));
  const hasMaterial = new Set<string>();
  for (const m of materials) {
    const dl = m.deadlineId ? deadlineById.get(m.deadlineId) : undefined;
    if (m.deadlineId && dl && (dl.status !== 'active' || dl.dueAt <= today)) continue; // deadline passed or done
    const key = dl ? dl.id : `self:${m.courseId}`;
    if (dl) hasMaterial.add(dl.id);
    if (!groups.has(key)) {
      groups.set(key, { key, courseId: m.courseId, start: today, end: addDays(today, SELF_PACED_DAYS - 1), buffer: 0, units: [], required: 0 });
    }
    const total = estimateMinutes(m, courseById.get(m.courseId));
    const credited = creditBy.get(m.id) ?? 0;
    const remaining = m.done ? 0 : Math.max(0, total - credited - (fixedFutureBy.get(m.id) ?? 0));
    groups.get(key)!.units.push({
      materialId: m.id,
      title: m.title,
      order: m.order,
      availableFrom: m.availableFrom && m.availableFrom > today ? m.availableFrom : today,
      total: m.done ? Math.max(total, credited) : total,
      credited: m.done ? Math.max(total, credited) : credited,
      remaining,
      catchup: missedBy.has(m.id),
    });
  }
  for (const d of activeDeadlines) {
    if (hasMaterial.has(d.id)) continue;
    const id = `auto:${d.id}`;
    const total = DEFAULT_MINUTES[d.kind];
    const credited = creditBy.get(id) ?? 0;
    groups.get(d.id)!.units.push({
      materialId: id,
      title: `Prepare: ${d.title}`,
      order: 0,
      availableFrom: today,
      total,
      credited,
      remaining: Math.max(0, total - credited - (fixedFutureBy.get(id) ?? 0)),
      catchup: missedBy.has(id),
    });
  }
  for (const g of groups.values()) {
    g.required = g.units.reduce((a, u) => a + u.remaining, 0);
    const unitStart = g.units.filter((u) => u.remaining > 0).map((u) => u.availableFrom).sort()[0];
    g.start = unitStart ?? today;
  }

  const ordered = [...groups.values()].sort((a, b) => {
    const sa = a.deadline ? 0 : 1;
    const sb = b.deadline ? 0 : 1;
    if (sa !== sb) return sa - sb;
    const da = a.deadline?.dueAt ?? a.end;
    const db = b.deadline?.dueAt ?? b.end;
    return da.localeCompare(db) || a.key.localeCompare(b.key);
  });

  const lastDue = ordered.reduce((acc, g) => (g.deadline && g.deadline.dueAt > acc ? g.deadline.dueAt : acc), addDays(today, SELF_PACED_DAYS));
  const horizon = dateRange(today, lastDue);
  const baseCap = new Map(horizon.map((d) => [d, baseCapacity(d, input)]));

  const setWindow = (g: Group, buffer: number) => {
    if (!g.deadline) return;
    const lastDay = addDays(g.deadline.dueAt, -1);
    let end = addDays(g.deadline.dueAt, -buffer);
    if (end < g.start) end = lastDay; // window too short: drop the buffer
    g.buffer = diffDays(end, g.deadline.dueAt);
    g.end = end;
  };

  const freshDays = (): Map<ISODate, Day> => {
    const days = new Map<ISODate, Day>();
    for (const d of horizon) days.set(d, { date: d, base: baseCap.get(d)!, cap: baseCap.get(d)!, used: 0, courses: new Set() });
    for (const s of fixed) {
      const day = days.get(s.date);
      if (!day) continue;
      day.used += sessionLoad(s);
      day.courses.add(s.courseId);
    }
    for (const day of days.values()) if (day.cap - day.used < settings.minChunk) day.cap = Math.min(day.cap, day.used);
    return days;
  };

  // 6. Water-fill one group into the day states; returns chunks placed.
  const fill = (g: Group, days: Map<ISODate, Day>) => {
    const chunks: { date: ISODate; minutes: number }[] = [];
    const retained: Session[] = [];
    const unitRem = new Map(g.units.map((u) => [u.materialId, u.remaining]));
    const unitFrom = new Map(g.units.map((u) => [u.materialId, u.availableFrom]));
    for (const s of previous) {
      if ((s.deadlineId ?? `self:${s.courseId}`) !== g.key || s.date > g.end) continue;
      const rem = unitRem.get(s.materialId) ?? 0;
      const day = days.get(s.date);
      if (rem <= 0 || !day || s.date < unitFrom.get(s.materialId)!) continue;
      const take = Math.min(s.minutes, rem);
      if (day.cap - day.used < take) continue;
      if (!day.courses.has(g.courseId) && day.courses.size >= settings.maxSubjects) continue;
      day.used += take;
      day.courses.add(g.courseId);
      unitRem.set(s.materialId, rem - take);
      retained.push({ ...s, minutes: take });
    }
    let R = [...unitRem.values()].reduce((a, b) => a + b, 0);
    if (R <= 0 || g.end < g.start) return { chunks, retained, unitRem, unplaced: Math.max(0, R) };
    const window = dateRange(g.start, g.end);
    const eligible = window.filter((d) => (days.get(d)?.cap ?? 0) - (days.get(d)?.used ?? 0) >= Math.min(R, settings.minChunk));
    // Spread over as many days as possible, but keep chunks between minChunk and maxSession.
    const n = Math.max(1, Math.ceil(R / settings.maxSession), Math.min(eligible.length, Math.floor(R / settings.minChunk)));
    const chunkSize = Math.min(settings.maxSession, Math.max(settings.minChunk, Math.ceil(R / n / 5) * 5));
    let i = 0;
    while (R > 0) {
      const want = Math.min(chunkSize, R);
      // Aim chunk i at an evenly spaced point in the window; load level wins over position.
      const target = window.length * ((Math.min(i, n - 1) + 0.5) / n);
      let best: { day: Day; take: number; score: number } | null = null;
      for (let idx = 0; idx < window.length; idx++) {
        const day = days.get(window[idx]);
        if (!day) continue;
        const take = Math.min(want, day.cap - day.used);
        if (take <= 0 || take < Math.min(want, settings.minChunk)) continue;
        if (!day.courses.has(g.courseId) && day.courses.size >= settings.maxSubjects) continue;
        const load = Math.round(((day.used + take) / Math.max(day.base, 1)) * 4) / 4;
        const score = load * 1000 + Math.abs(idx + 0.5 - target);
        if (!best || score < best.score) best = { day, take, score };
      }
      if (!best) break;
      const { day, take } = best;
      day.used += take;
      day.courses.add(g.courseId);
      chunks.push({ date: day.date, minutes: take });
      R -= take;
      i++;
    }
    return { chunks, retained, unitRem, unplaced: R };
  };

  // 7. Triage: first pass with buffers, then shrink buffers for deadlines that didn't fit.
  for (const g of ordered) setWindow(g, g.buffer);
  let days = freshDays();
  let placements = new Map<string, ReturnType<typeof fill>>();
  for (const g of ordered) placements.set(g.key, fill(g, days));
  const shrunk = new Set<string>();
  for (const g of ordered) {
    if (g.deadline && placements.get(g.key)!.unplaced > 0 && g.buffer > 1) shrunk.add(g.key);
  }
  if (shrunk.size) {
    for (const g of ordered) if (shrunk.has(g.key)) setWindow(g, 1);
    days = freshDays();
    placements = new Map();
    for (const g of ordered) placements.set(g.key, fill(g, days));
  }

  // Assign materials (in syllabus order) to each group's chunks, earliest first.
  const created: Session[] = [];
  const lastLearn = new Map<string, ISODate>();
  for (const s of kept) {
    if (s.kind === 'new' && (s.status === 'done' || s.status === 'partial' || s.status === 'planned')) {
      if (!lastLearn.has(s.materialId) || lastLearn.get(s.materialId)! < s.date) lastLearn.set(s.materialId, s.date);
    }
  }
  const ordinal = new Map<string, number>();
  const makeId = (materialId: string, date: ISODate, kind: string) => {
    const k = `${materialId}|${date}|${kind}`;
    const n = (ordinal.get(k) ?? 0) + 1;
    ordinal.set(k, n);
    return `s${hash(`${k}|${n}`)}`;
  };

  for (const g of ordered) {
    const { chunks, retained, unitRem } = placements.get(g.key)!;
    const units = g.units.map((u) => ({ ...u, remaining: unitRem.get(u.materialId) ?? 0 }));
    const catchup = new Set(units.filter((u) => u.catchup).map((u) => u.materialId));
    const totalMin = units.reduce((a, u) => a + u.total, 0);
    let doneMin = units.reduce((a, u) => a + Math.min(u.total, u.credited), 0);
    const reasonsFor = (date: ISODate, materialId: string): Reason[] => {
      const reasons: Reason[] = [];
      if (g.deadline) {
        reasons.push({ code: 'deadline', title: g.deadline.title, kind: g.deadline.kind, daysLeft: diffDays(date, g.deadline.dueAt) });
        if (shrunk.has(g.key)) reasons.push({ code: 'bufferShrunk' });
      } else reasons.push({ code: 'selfPaced' });
      reasons.push({ code: 'progress', doneMin, totalMin });
      if (catchup.has(materialId)) reasons.push({ code: 'catchup' });
      return reasons;
    };
    const slots = [...retained.map((r) => ({ date: r.date, minutes: r.minutes, keep: r })), ...chunks.map((c) => ({ ...c, keep: undefined as Session | undefined }))].sort(
      (a, b) => a.date.localeCompare(b.date) || (a.keep ? 0 : 1) - (b.keep ? 0 : 1),
    );
    for (const c of slots) {
      if (c.keep) {
        created.push({ ...c.keep, reasons: reasonsFor(c.date, c.keep.materialId) });
        ordinal.set(`${c.keep.materialId}|${c.date}|new`, (ordinal.get(`${c.keep.materialId}|${c.date}|new`) ?? 0) + 1);
        doneMin += c.minutes;
        if (!lastLearn.has(c.keep.materialId) || lastLearn.get(c.keep.materialId)! < c.date) lastLearn.set(c.keep.materialId, c.date);
        continue;
      }
      let M = c.minutes;
      while (M > 0) {
        const u = units.find((x) => x.remaining > 0 && x.availableFrom <= c.date);
        if (!u) break;
        const take = Math.min(M, u.remaining);
        const reasons = reasonsFor(c.date, u.materialId);
        const prev = created.find((s) => s.materialId === u.materialId && s.date === c.date && s.kind === 'new');
        if (prev) prev.minutes += take;
        else {
          created.push({
            id: makeId(u.materialId, c.date, 'new'),
            date: c.date,
            materialId: u.materialId,
            deadlineId: g.deadline?.id,
            courseId: g.courseId,
            kind: 'new',
            minutes: take,
            pinned: false,
            status: 'planned',
            reasons,
          });
        }
        u.remaining -= take;
        doneMin += take;
        M -= take;
        if (!lastLearn.has(u.materialId) || lastLearn.get(u.materialId)! < c.date) lastLearn.set(u.materialId, c.date);
      }
    }
  }

  // 8. Reviews use only leftover capacity: spaced reviews, then one review per buffer day.
  const firstFree = today;
  const tryPlace = (date: ISODate, courseId: string, want: number, min: number): number => {
    const day = days.get(date);
    if (!day || date < firstFree) return 0;
    if (!day.courses.has(courseId) && day.courses.size >= settings.maxSubjects) return 0;
    const take = Math.min(want, day.cap - day.used);
    if (take < min) return 0;
    day.used += take;
    day.courses.add(courseId);
    return take;
  };
  const existingReview = new Set(kept.filter((s) => s.kind === 'review').map((s) => `${s.deadlineId}|${s.date}`));
  for (const g of ordered) {
    const dl = g.deadline;
    if (!dl || dl.kind === 'assignment') continue;
    const byDate = new Map<ISODate, { ids: string[]; minutes: number; offset: number }>();
    for (const u of g.units) {
      const learned = lastLearn.get(u.materialId);
      if (!learned) continue;
      for (const off of REVIEW_OFFSETS) {
        const rd = addDays(learned, off);
        if (rd < firstFree || rd >= dl.dueAt) continue;
        const e = byDate.get(rd) ?? { ids: [], minutes: 0, offset: off };
        e.ids.push(u.materialId);
        e.minutes += u.total * REVIEW_SHARE;
        e.offset = Math.min(e.offset, off);
        byDate.set(rd, e);
      }
    }
    const bufferDays = dateRange(addDays(g.end, 1), addDays(dl.dueAt, -1));
    const totalMin = g.units.reduce((a, u) => a + u.total, 0);
    for (const date of [...byDate.keys()].sort()) {
      if (bufferDays.includes(date) || existingReview.has(`${dl.id}|${date}`)) continue;
      const e = byDate.get(date)!;
      const got = tryPlace(date, g.courseId, Math.min(settings.maxSession, round5(e.minutes)), 10);
      if (got) {
        created.push({
          id: makeId(e.ids[0], date, 'review'),
          date,
          materialId: e.ids[0],
          reviewMaterialIds: e.ids,
          deadlineId: dl.id,
          courseId: g.courseId,
          kind: 'review',
          minutes: got,
          pinned: false,
          status: 'planned',
          reasons: [{ code: 'review', offset: e.offset }, { code: 'deadline', title: dl.title, kind: dl.kind, daysLeft: diffDays(date, dl.dueAt) }],
        });
      }
    }
    for (const date of bufferDays) {
      if (existingReview.has(`${dl.id}|${date}`)) continue;
      const want = Math.min(settings.maxSession, Math.max(30, round5(totalMin * REVIEW_SHARE)));
      const got = tryPlace(date, g.courseId, want, 15);
      if (got) {
        const ids = g.units.map((u) => u.materialId);
        created.push({
          id: makeId(ids[0], date, 'review'),
          date,
          materialId: ids[0],
          reviewMaterialIds: ids,
          deadlineId: dl.id,
          courseId: g.courseId,
          kind: 'review',
          minutes: got,
          pinned: false,
          status: 'planned',
          reasons: [{ code: 'bufferReview', title: dl.title }, { code: 'deadline', title: dl.title, kind: dl.kind, daysLeft: diffDays(date, dl.dueAt) }],
        });
      }
    }
  }

  // 9. Health and fixes.
  const healthRows: DeadlineHealth[] = [];
  for (const g of ordered) {
    const dl = g.deadline;
    if (!dl) continue;
    const p = placements.get(g.key)!;
    const totalMin = g.units.reduce((a, u) => a + u.total, 0);
    const doneMin = g.units.reduce((a, u) => a + Math.min(u.total, u.credited), 0);
    const fixedMin = g.units.reduce((a, u) => a + (fixedFutureBy.get(u.materialId) ?? 0), 0);
    const plannedMin = p.chunks.reduce((a, c) => a + c.minutes, 0) + p.retained.reduce((a, r) => a + r.minutes, 0) + fixedMin;
    const shortfallMin = p.unplaced;
    healthRows.push({
      deadlineId: dl.id,
      title: dl.title,
      dueAt: dl.dueAt,
      totalMin,
      doneMin,
      plannedMin,
      shortfallMin,
      bufferShrunk: shrunk.has(g.key),
      fixes: shortfallMin > 0 ? suggestFixes(g, shortfallMin, input) : [],
    });
  }
  const status: Health['status'] = healthRows.some((h) => h.shortfallMin > 0) ? 'atRisk' : healthRows.some((h) => h.bufferShrunk) ? 'tight' : 'ok';

  const sessions = [...kept.map((s) => (s.pinned && !s.reasons.some((r) => r.code === 'pinned') ? { ...s, reasons: [{ code: 'pinned' as const }, ...s.reasons] } : s)), ...created].sort(
    (a, b) => a.date.localeCompare(b.date) || (a.kind === b.kind ? 0 : a.kind === 'new' ? -1 : 1) || a.id.localeCompare(b.id),
  );
  return { sessions, health: { status, deadlines: healthRows } };
}

function suggestFixes(g: Group, shortfall: number, input: PlanInput): string[] {
  const fixes: string[] = [];
  const window = g.end >= g.start ? dateRange(g.start, g.end) : [];
  const dl = g.deadline!;
  if (!window.length) return [`${dl.title} is too close to schedule new study. Lower the estimate or mark material as done.`];
  const slack = input.settings.slack;
  const blocked = window.filter((d) => isBlocked(d, input.blocks) && (input.availability.minutesByWeekday[weekday(d)] ?? 0) > 0);
  if (blocked.length) {
    const gain = blocked.reduce((a, d) => a + Math.floor((input.availability.minutesByWeekday[weekday(d)] ?? 0) * slack), 0);
    fixes.push(`Unblock ${blocked.length} blocked day${blocked.length > 1 ? 's' : ''} before ${dl.title} (+${gain} min)`);
  }
  const zeroDays = new Map<number, number>();
  for (const d of window) if (!isBlocked(d, input.blocks) && !(input.availability.minutesByWeekday[weekday(d)] > 0)) zeroDays.set(weekday(d), (zeroDays.get(weekday(d)) ?? 0) + 1);
  for (const [wd, count] of [...zeroDays].sort((a, b) => b[1] - a[1]).slice(0, 2)) {
    fixes.push(`Study 60 min on ${WEEKDAYS[wd]}s (+${Math.floor(60 * slack) * count} min)`);
  }
  const studyDays = window.filter((d) => !isBlocked(d, input.blocks) && input.availability.minutesByWeekday[weekday(d)] > 0).length;
  if (studyDays) fixes.push(`Add ${round5(shortfall / studyDays / slack)} min per study day until ${addDays(g.end, 0)}`);
  fixes.push(`Lower the estimate for ${dl.title} or mark some material as done`);
  return fixes;
}

/** Compares the future planned sessions of two plans. */
export function diffPlans(before: Session[], after: Session[], today: ISODate): PlanDiff {
  const key = (s: Session) => `${s.materialId}|${s.date}|${s.kind}`;
  const fut = (list: Session[]) => new Map(list.filter((s) => s.date >= today && s.status === 'planned').map((s) => [key(s), s]));
  const a = fut(before);
  const b = fut(after);
  let added = 0;
  let removed = 0;
  let changed = 0;
  for (const [k, s] of b) {
    const old = a.get(k);
    if (!old) added++;
    else if (old.minutes !== s.minutes) changed++;
  }
  for (const k of a.keys()) if (!b.has(k)) removed++;
  const parts = [added && `${added} added`, removed && `${removed} removed`, changed && `${changed} resized`].filter(Boolean);
  return { added, removed, changed, summary: parts.length ? parts.join(', ') : 'No changes' };
}
