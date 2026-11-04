import { fromNodeHeaders, toNodeHandler } from 'better-auth/node';
import crypto from 'node:crypto';
import { and, eq, gte, sql } from 'drizzle-orm';
import express, { type NextFunction, type Request, type Response } from 'express';
import rateLimit from 'express-rate-limit';
import helmet from 'helmet';
import fs from 'node:fs';
import path from 'node:path';
import { APP_NAME } from '../shared/brand';
import { addDays } from '../shared/dates';
import { GRADE_SCALES, GradeError, normalizeGrade } from '../shared/grades';
import type { Deadline, Difficulty, ExamResult, GradeValue, Material, PublicState, PublicUser, Session, Source, TopicRating, UnitType, UserState } from '../shared/types';
import { updateCalibration } from '../planner/estimate';
import { learnedFactors } from '../planner/learning';
import { registerAiRoutes } from './ai/routes';
import type { Auth } from './auth';
import { parseMaterialLine } from './classify';
import { connectUrl, disconnectGoogle, finishConnect, googleApi, googleStatus, listCalendars } from './connectors/google';
import { randomId } from './crypto';
import { withUserLock, type DB } from './db';
import * as s from './db/schema';
import { buildDigest } from './digest';
import { env, features } from './env';
import { exportIcs } from './ics-export';
import { tick } from './jobs';
import { createFromItem, DEFAULT_INTERVAL, findOrCreateCourse } from './merge';
import { replan, snapshot, todayFor, undo } from './plan-service';
import { sendPush } from './push';
import { loadState, saveState } from './repo';
import { effortFor, targetShare } from './results';
import { syncAllForUser, syncSource } from './sync';
import { BadRequest, date, need, num, oneOf, optStr, str } from './validate';

declare global {
  // eslint-disable-next-line @typescript-eslint/no-namespace
  namespace Express {
    interface Request {
      user?: PublicUser;
    }
  }
}

export interface AppDeps {
  db: DB;
  auth: Auth;
}

const UNIT_TYPES: UnitType[] = ['pages', 'slides', 'problems', 'videoMinutes', 'topics', 'minutes'];
const KINDS = ['exam', 'quiz', 'assignment'] as const;
const RATINGS: TopicRating[] = ['weak', 'ok', 'strong', 'not_on_exam'];

export interface MutationCtx {
  tx: DB;
  userId: string;
  req: Request;
}
export type Mutation = (state: UserState, ctx: MutationCtx) => void | Promise<void>;

export function createApp({ db, auth }: AppDeps) {
  const app = express();
  app.disable('x-powered-by');
  app.set('trust proxy', env.trustProxy);
  app.use(
    helmet({
      contentSecurityPolicy: {
        directives: {
          defaultSrc: ["'self'"],
          scriptSrc: ["'self'"],
          styleSrc: ["'self'", "'unsafe-inline'"],
          imgSrc: ["'self'", 'data:', 'https://*.googleusercontent.com'],
          connectSrc: ["'self'"],
          fontSrc: ["'self'", 'data:'],
          objectSrc: ["'none'"],
          frameAncestors: ["'none'"],
          workerSrc: ["'self'"],
          manifestSrc: ["'self'"],
          upgradeInsecureRequests: env.isProd ? [] : null,
        },
      },
      crossOriginEmbedderPolicy: false,
    }),
  );

  // The client address as Express resolves it through the trusted proxies. Always overwritten,
  // so a client can't spoof it; Better Auth reads it for per-user rate limits.
  app.use((req, _res, next) => {
    if (req.ip) req.headers['x-cramless-client-ip'] = req.ip;
    else delete req.headers['x-cramless-client-ip'];
    next();
  });

  // Better Auth handles its own body parsing, CSRF (origin) checks and rate limits.
  app.all('/api/auth/*splat', toNodeHandler(auth));

  app.use(express.json({ limit: '1mb' }));

  // Defense in depth against CSRF: state-changing API calls must come from our own origin.
  const devOrigin = (o: string) => !env.isProd && /^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(o);
  app.use('/api', (req, res, next) => {
    if (['GET', 'HEAD', 'OPTIONS'].includes(req.method) || req.path.startsWith('/cron/')) return next();
    const origin = req.get('origin');
    if (origin && origin !== env.publicUrl && !devOrigin(origin)) return res.status(403).json({ error: 'Request blocked (wrong origin).' });
    next();
  });
  app.use(
    '/api',
    rateLimit({ windowMs: 5 * 60_000, limit: 600, standardHeaders: 'draft-8', legacyHeaders: false, message: { error: 'Too many requests. Please slow down.' } }),
  );

  // ---------- Public endpoints ----------
  app.get('/api/health', async (_req, res) => {
    try {
      await db.execute(sql`select 1`);
      res.json({ ok: true });
    } catch {
      res.status(503).json({ ok: false });
    }
  });

  app.get('/api/config', (_req, res) => {
    res.json({
      appName: APP_NAME,
      googleSignIn: features.google(),
      emailSignUp: features.email() || !features.google(),
      emailVerification: features.email(),
      passwordReset: features.email(),
      supportEmail: env.supportEmail ?? null,
      googleClientId: env.google.clientId ?? null,
    });
  });

  app.post('/api/cron/tick', async (req, res) => {
    const token = (req.get('authorization') ?? '').replace(/^Bearer\s+/i, '');
    const ok = !!env.cronSecret && token.length === env.cronSecret.length && crypto.timingSafeEqual(Buffer.from(token), Buffer.from(env.cronSecret));
    if (!ok) return res.status(401).json({ error: 'Unauthorized' });
    res.json(await tick(db, 25_000));
  });

  // ---------- Auth gate ----------
  const requireUser = async (req: Request, res: Response, next: NextFunction) => {
    try {
      const session = await auth.api.getSession({ headers: fromNodeHeaders(req.headers) });
      if (!session) return res.status(401).json({ error: 'Please sign in.' });
      const u = session.user;
      req.user = { id: u.id, name: u.name, email: u.email, emailVerified: u.emailVerified, image: u.image ?? null };
      next();
    } catch (e) {
      next(e);
    }
  };

  const aiUsedToday = async (userId: string) => {
    const since = new Date(Date.now() - 24 * 3600_000);
    const [row] = await db
      .select({ n: sql<number>`count(*)::int` })
      .from(s.aiUsage)
      .where(and(eq(s.aiUsage.userId, userId), gte(s.aiUsage.createdAt, since), sql`${s.aiUsage.model} <> 'cache'`));
    return Number(row?.n ?? 0);
  };

  const publicState = async (state: UserState, user: PublicUser): Promise<PublicState & { learning: ReturnType<typeof learnedFactors> }> => {
    const today = todayFor(state);
    const { snapshots, ...rest } = state;
    const google = features.google() ? await googleStatus(db, user.id) : { connected: false };
    return {
      ...rest,
      sessions: rest.sessions.filter((x) => x.date >= addDays(today, -60)),
      inbox: rest.inbox.filter((e) => !e.resolved),
      digestLog: rest.digestLog.slice(-7),
      canUndo: snapshots.at(-1)?.label ?? null,
      today,
      user,
      google: { available: features.google(), ...google },
      features: { ai: aiAllowedFor(state), push: features.push(), email: features.email() },
      isAdmin: isAdmin(user),
      vapidPublicKey: env.push.publicKey,
      ai: { usedToday: await aiUsedToday(user.id), dailyLimit: env.anthropic.dailyLimit },
      learning: learnedFactors(state.results, state.courses, state.settings),
    };
  };

  const fail = (res: Response, e: unknown) => {
    if (e instanceof BadRequest || e instanceof GradeError) return res.status(400).json({ error: (e as Error).message });
    console.error('[api]', e);
    res.status(500).json({ error: env.isProd ? 'Something went wrong. Please try again.' : (e as Error).message });
  };

  /** A user change: lock the user, snapshot for undo, apply, re-plan, save the diff. */
  const applyMutation = (req: Request, label: string | null, fn: Mutation): Promise<UserState> => {
    const userId = req.user!.id;
    return withUserLock(db, userId, async (tx) => {
      const before = await loadState(tx, userId);
      const after = structuredClone(before);
      if (label) snapshot(after, label);
      await fn(after, { tx, userId, req });
      replan(after, label ?? undefined);
      await saveState(tx, userId, before, after);
      return after;
    });
  };

  const mutate = (label: string | null, fn: Mutation) => async (req: Request, res: Response) => {
    try {
      const state = await applyMutation(req, label, fn);
      res.json(await publicState(state, req.user!));
    } catch (e) {
      fail(res, e);
    }
  };

  const respondState = async (req: Request, res: Response) => {
    const userId = req.user!.id;
    let state = await loadState(db, userId);
    if (state.planMeta.lastPlanDate !== todayFor(state)) {
      state = await withUserLock(db, userId, async (tx) => {
        const before = await loadState(tx, userId);
        const after = structuredClone(before);
        replan(after);
        await saveState(tx, userId, before, after);
        return after;
      });
    }
    res.json(await publicState(state, req.user!));
  };

  const api = express.Router();
  api.use(requireUser);

  api.get('/state', async (req, res) => {
    try {
      await respondState(req, res);
    } catch (e) {
      fail(res, e);
    }
  });

  /** Neutral age screen (birth month and year). Under 13: the account is deleted right away. */
  api.post('/age', async (req, res) => {
    try {
      const bm = String(req.body?.birthMonth ?? '');
      need(/^\d{4}-(0[1-9]|1[0-2])$/.test(bm), 'Choose your birth month and year');
      const year = Number(bm.slice(0, 4));
      need(year >= 1900 && year <= new Date().getUTCFullYear(), 'Choose your birth month and year');
      if (ageInYears(bm) < 13) {
        await disconnectGoogle(db, req.user!.id);
        await db.delete(s.user).where(eq(s.user.id, req.user!.id));
        return res.json({ deleted: true });
      }
      const state = await applyMutation(req, null, (st) => {
        need(!st.settings.birthMonth, 'Your birth month is already set');
        st.settings.birthMonth = bm;
      });
      res.json(await publicState(state, req.user!));
    } catch (e) {
      fail(res, e);
    }
  });

  /** Owner-only totals for running the service (no personal data). */
  api.get('/admin/status', async (req, res) => {
    if (!isAdmin(req.user!)) return res.status(404).json({ error: 'Not found' });
    const one = async (q: ReturnType<typeof sql>) => {
      const r: any = await db.execute(q);
      return (r.rows ?? r)[0] ?? {};
    };
    const monthStart = new Date(Date.UTC(new Date().getUTCFullYear(), new Date().getUTCMonth(), 1));
    res.json({
      users: await one(sql`select count(*)::int as total, count(*) filter (where email_verified)::int as verified from "user"`),
      activeLast7Days: (await one(sql`select count(*)::int as n from profile where updated_at > now() - interval '7 days'`)).n,
      sources: await one(sql`select count(*)::int as total,
        count(*) filter (where needs_reconnect)::int as needs_reconnect,
        count(*) filter (where status = 'error')::int as erroring,
        count(*) filter (where next_sync_at < now() - interval '30 minutes' and not needs_reconnect)::int as overdue
        from source`),
      remindersLast24h: await one(sql`select count(*) filter (where channel = 'push')::int as push, count(*) filter (where channel = 'email')::int as email from digest_log where sent_at > now() - interval '24 hours'`),
      overdueReminders: (await one(sql`select count(*)::int as n from profile where next_digest_at < now() - interval '30 minutes'`)).n,
      push: await one(sql`select count(*)::int as devices, count(*) filter (where failure_count > 0)::int as failing from push_subscription`),
      ai: {
        ...(await one(sql`select count(*)::int as calls, (coalesce(sum(cost_micros), 0) / 1e6)::float as spent_usd from ai_usage where created_at >= ${monthStart}`)),
        budgetUsd: env.anthropic.monthlyBudgetUsd,
        model: features.ai() ? env.anthropic.model : null,
      },
      request: { ip: req.ip, forwardedFor: req.get('x-forwarded-for') ?? null, trustProxy: env.trustProxy },
    });
  });

  api.post(
    '/onboarding',
    mutate(null, (st, { req }) => {
      const tz = req.body?.timezone;
      if (typeof tz === 'string' && validTz(tz)) st.settings.timezone = tz;
      st.settings.onboarded = true;
    }),
  );

  api.put(
    '/settings',
    mutate(null, (st, { req }) => {
      const b = req.body ?? {};
      const set = st.settings;
      if (b.timezone != null) {
        need(validTz(b.timezone), 'Unknown time zone');
        set.timezone = b.timezone;
      }
      if (b.digestTime != null) {
        need(/^\d{2}:\d{2}$/.test(b.digestTime), 'Reminder time must be HH:MM');
        set.digestTime = b.digestTime;
      }
      if (b.slack != null) set.slack = num(b.slack, 0.5, 1, 'Fill level');
      if (b.minChunk != null) set.minChunk = num(b.minChunk, 10, 60, 'Shortest session');
      if (b.maxSession != null) set.maxSession = num(b.maxSession, 20, 180, 'Longest session');
      if (b.maxSubjects != null) set.maxSubjects = num(b.maxSubjects, 1, 6, 'Subjects per day');
      need(set.minChunk <= set.maxSession, 'Shortest session must not exceed the longest');
      if (b.gradeScale != null) set.gradeScale = oneOf(b.gradeScale, GRADE_SCALES.map((g) => g.id), 'Grade scale');
      if (b.targetGrade !== undefined) set.targetGrade = b.targetGrade === null ? undefined : gradeValue(b.targetGrade);
      if (b.reminders) set.reminders = { push: !!b.reminders.push, email: !!b.reminders.email };
    }),
  );

  api.put(
    '/availability',
    mutate('Changed weekly availability', (st, { req }) => {
      const b = req.body ?? {};
      need(Array.isArray(b.minutesByWeekday) && b.minutesByWeekday.length === 7, 'minutesByWeekday needs 7 values');
      st.availability.minutesByWeekday = b.minutesByWeekday.map((m: unknown, i: number) => num(m, 0, 960, `Day ${i}`));
      if (b.studyWindow === null) delete st.availability.studyWindow;
      else if (b.studyWindow) {
        need(/^\d{2}:\d{2}$/.test(b.studyWindow.start) && /^\d{2}:\d{2}$/.test(b.studyWindow.end) && b.studyWindow.start < b.studyWindow.end, 'Study window must be a valid time range');
        st.availability.studyWindow = { start: b.studyWindow.start, end: b.studyWindow.end };
      }
    }),
  );

  api.post(
    '/blocks',
    mutate('Blocked dates', (st, { req }) => {
      const start = date(req.body?.start, 'Start');
      const end = req.body?.end ? date(req.body.end, 'End') : start;
      need(end >= start, 'End must be on or after start');
      need(st.blocks.length < 200, 'Too many blocked periods');
      st.blocks.push({ id: randomId('b_'), start, end, reason: optStr(req.body?.reason, 100) });
    }),
  );

  api.delete(
    '/blocks/:id',
    mutate('Removed a blocked period', (st, { req }) => {
      st.blocks = st.blocks.filter((b) => b.id !== req.params.id);
    }),
  );

  api.post(
    '/today/off',
    mutate("Can't study today", (st) => {
      const today = todayFor(st);
      st.blocks.push({ id: randomId('b_'), start: today, end: today, reason: "Can't study today" });
      for (const x of st.sessions) if (x.date === today && x.status === 'planned') x.status = 'skipped';
    }),
  );

  const courseFrom = (st: UserState, b: any) => {
    if (b.courseId) {
      const c = st.courses.find((x) => x.id === b.courseId);
      need(c, 'Course not found');
      return c!;
    }
    need(st.courses.length < 100, 'Too many courses');
    return findOrCreateCourse(st, str(b.courseName, 'Course', 80));
  };

  api.post(
    '/courses',
    mutate('Added a course', (st, { req }) => {
      courseFrom(st, { courseName: req.body?.name });
    }),
  );

  api.put(
    '/courses/:id',
    mutate('Edited a course', (st, { req }) => {
      const c = st.courses.find((x) => x.id === req.params.id);
      need(c, 'Course not found');
      const b = req.body ?? {};
      if (b.name) c!.name = str(b.name, 'Course name', 80);
      if (b.color && /^#[0-9a-f]{6}$/i.test(b.color)) c!.color = b.color;
      if (b.targetGrade !== undefined) c!.targetGrade = b.targetGrade === null ? undefined : gradeValue(b.targetGrade);
      if (b.gradeScale !== undefined) c!.gradeScale = b.gradeScale === null ? undefined : oneOf(b.gradeScale, GRADE_SCALES.map((g) => g.id), 'Grade scale');
      if (b.learningLocked != null) c!.learningLocked = !!b.learningLocked;
    }),
  );

  api.delete(
    '/courses/:id',
    mutate('Deleted a course', (st, { req }) => {
      const id = req.params.id;
      st.courses = st.courses.filter((c) => c.id !== id);
      st.deadlines = st.deadlines.filter((d) => d.courseId !== id);
      st.materials = st.materials.filter((m) => m.courseId !== id);
      st.sessions = st.sessions.filter((x) => x.courseId !== id || x.status !== 'planned');
    }),
  );

  api.post(
    '/deadlines',
    mutate('Added an exam or deadline', (st, { req }) => {
      const b = req.body ?? {};
      const course = courseFrom(st, b);
      need(st.deadlines.length < 2000, 'Too many deadlines');
      const d: Deadline = {
        id: randomId('d_'),
        courseId: course.id,
        kind: oneOf(b.kind, KINDS, 'Type'),
        title: str(b.title, 'Title'),
        dueAt: date(b.dueAt, 'Date'),
        status: 'active',
        userEdited: [],
        confidence: 1,
        timeAdjust: 1,
      };
      if (b.roughMinutes != null && b.roughMinutes !== '') {
        d.roughMinutes = num(b.roughMinutes, 5, 6000, 'Time');
        d.roughSource = b.roughSource === 'history' ? 'history' : 'user';
      }
      st.deadlines.push(d);
    }),
  );

  api.put(
    '/deadlines/:id',
    mutate('Edited a deadline', (st, { req }) => {
      const d = st.deadlines.find((x) => x.id === req.params.id);
      need(d, 'Deadline not found');
      const b = req.body ?? {};
      const edit = (field: 'title' | 'dueAt' | 'kind', value: any) => {
        if (d![field] === value) return;
        (d as any)[field] = value;
        if (d!.origin && !d!.userEdited.includes(field)) d!.userEdited.push(field);
      };
      if (b.title != null) edit('title', str(b.title, 'Title'));
      if (b.dueAt != null) edit('dueAt', date(b.dueAt, 'Date'));
      if (b.kind != null) edit('kind', oneOf(b.kind, KINDS, 'Type'));
      if (b.status != null) d!.status = oneOf(b.status, ['active', 'done'] as const, 'Status');
      if (b.courseId) d!.courseId = courseFrom(st, b).id;
      if (b.timeAdjust != null) d!.timeAdjust = num(b.timeAdjust, 0.25, 3, 'Time adjustment');
      if (b.roughMinutes === null || b.roughMinutes === '') {
        delete d!.roughMinutes;
        delete d!.roughSource;
      } else if (b.roughMinutes != null) {
        d!.roughMinutes = num(b.roughMinutes, 5, 6000, 'Time');
        d!.roughSource = b.roughSource === 'history' ? 'history' : 'user';
      }
      if (b.targetGrade !== undefined) d!.targetGrade = b.targetGrade === null ? undefined : gradeValue(b.targetGrade);
    }),
  );

  api.post(
    '/deadlines/:id/link',
    mutate('Linked material to an exam', (st, { req }) => {
      const d = st.deadlines.find((x) => x.id === req.params.id);
      need(d, 'Deadline not found');
      const ids = new Set<string>(Array.isArray(req.body?.materialIds) ? req.body.materialIds.map(String) : []);
      for (const m of st.materials) if (ids.has(m.id) && m.courseId === d!.courseId) m.deadlineId = d!.id;
    }),
  );

  api.delete(
    '/deadlines/:id',
    mutate('Deleted a deadline', (st, { req }) => {
      const id = req.params.id;
      st.deadlines = st.deadlines.filter((d) => d.id !== id);
      if (req.query.keepMaterials) {
        for (const m of st.materials) if (m.deadlineId === id) delete m.deadlineId;
      } else st.materials = st.materials.filter((m) => m.deadlineId !== id);
      st.sessions = st.sessions.filter((x) => x.deadlineId !== id || x.status !== 'planned');
    }),
  );

  const materialFields = (b: any, m: Partial<Material>) => {
    if (b.title != null) m.title = str(b.title, 'Title');
    if (b.unitType != null) m.unitType = oneOf(b.unitType, UNIT_TYPES, 'Unit');
    if (b.quantity != null) m.quantity = num(b.quantity, 0, 100000, 'Amount');
    if (b.difficulty != null) m.difficulty = num(b.difficulty, 1, 3, 'Difficulty') as Difficulty;
    if (b.timeAdjust != null) m.timeAdjust = num(b.timeAdjust, 0.25, 3, 'Time adjustment');
    if (b.estimateOverride === null || b.estimateOverride === '') delete m.estimateOverride;
    else if (b.estimateOverride != null) m.estimateOverride = num(b.estimateOverride, 0, 100000, 'Estimate');
    if (b.availableFrom === null || b.availableFrom === '') delete m.availableFrom;
    else if (b.availableFrom != null) m.availableFrom = date(b.availableFrom, 'Available from');
    if (b.done != null) m.done = !!b.done;
    if (b.weak != null) m.weak = !!b.weak;
    if (b.note !== undefined) m.note = optStr(b.note, 500);
  };

  api.post(
    '/materials',
    mutate('Added material', (st, { req }) => {
      const b = req.body ?? {};
      const course = courseFrom(st, b);
      const deadlineId = b.deadlineId || undefined;
      if (deadlineId) need(st.deadlines.some((d) => d.id === deadlineId), 'Deadline not found');
      const order0 = st.materials.filter((m) => m.courseId === course.id).length;
      type Row = { title: string; unitType: UnitType; quantity: number; difficulty?: number; note?: string } | null;
      const rows: Row[] = Array.isArray(b.items)
        ? (b.items as any[]).slice(0, 100).map((it) => ({
            title: str(it?.title, 'Title'),
            unitType: oneOf(it?.unitType, UNIT_TYPES, 'Unit'),
            quantity: num(it?.quantity, 0, 100000, 'Amount'),
            difficulty: it?.difficulty != null ? num(it.difficulty, 1, 3, 'Difficulty') : undefined,
            note: it?.startPage && it?.endPage ? `${it.unitType === 'slides' ? 'Slides' : 'Pages'} ${Number(it.startPage)}–${Number(it.endPage)}${b.fileName ? ` of ${String(b.fileName).slice(0, 120)}` : ''}` : undefined,
          }))
        : typeof b.lines === 'string'
          ? (b.lines as string).split(/\r?\n/).slice(0, 200).map(parseMaterialLine).filter((r): r is NonNullable<ReturnType<typeof parseMaterialLine>> => !!r)
          : [null];
      need(rows.length > 0, 'Paste at least one line');
      need(st.materials.length + rows.length <= 5000, 'Too many materials');
      rows.forEach((row, i) => {
        const m: Material = { id: randomId('m_'), courseId: course.id, deadlineId, title: '', unitType: 'topics', quantity: 1, difficulty: 2, timeAdjust: 1, order: order0 + i, done: false, userEdited: [] };
        materialFields(row ?? b, m);
        if (row && !row.difficulty && b.difficulty) m.difficulty = num(b.difficulty, 1, 3, 'Difficulty') as Difficulty;
        if (row?.note) m.note = row.note;
        need(m.title, 'Title is required');
        st.materials.push(m);
      });
    }),
  );

  api.put(
    '/materials/:id',
    mutate('Edited material', (st, { req }) => {
      const m = st.materials.find((x) => x.id === req.params.id);
      need(m, 'Material not found');
      const before = { ...m! };
      materialFields(req.body ?? {}, m!);
      if ('deadlineId' in (req.body ?? {})) {
        const id = req.body.deadlineId || undefined;
        if (id) need(st.deadlines.some((d) => d.id === id), 'Deadline not found');
        m!.deadlineId = id;
      }
      if (m!.origin) for (const f of ['title', 'unitType', 'quantity', 'deadlineId'] as const) if (before[f] !== m![f] && !m!.userEdited.includes(f)) m!.userEdited.push(f);
    }),
  );

  api.delete(
    '/materials/:id',
    mutate('Deleted material', (st, { req }) => {
      st.materials = st.materials.filter((m) => m.id !== req.params.id);
      st.sessions = st.sessions.filter((x) => x.materialId !== req.params.id || x.status !== 'planned');
    }),
  );

  api.put(
    '/sessions/:id',
    mutate('Edited a session', (st, { req }) => {
      const x = st.sessions.find((y) => y.id === req.params.id);
      need(x, 'Session not found');
      const b = req.body ?? {};
      if (b.status != null) {
        x!.status = oneOf(b.status, ['planned', 'done', 'partial', 'skipped'] as const, 'Status');
        if (b.status === 'planned') delete x!.actualMinutes;
      }
      if (b.actualMinutes != null && b.actualMinutes !== '') {
        x!.actualMinutes = num(b.actualMinutes, 0, 600, 'Actual minutes');
        // Learn pace only from coverage sessions whose length the app chose: not pinned or resized,
        // no exact override, and no "more/less time" the user asked for on the item or exam.
        const m = st.materials.find((y) => y.id === x!.materialId);
        const dl = m?.deadlineId ? st.deadlines.find((y) => y.id === m.deadlineId) : undefined;
        const adjusted = (m?.timeAdjust ?? 1) !== 1 || (dl?.timeAdjust ?? 1) < 1;
        if (x!.status === 'done' && x!.kind === 'new' && !x!.pinned && m && m.estimateOverride == null && !adjusted) {
          const i = st.courses.findIndex((c) => c.id === x!.courseId);
          if (i >= 0) st.courses[i] = updateCalibration(st.courses[i], x!.minutes, x!.actualMinutes, x!.pace);
        }
      }
      if (b.date != null && b.date !== x!.date) {
        x!.date = date(b.date, 'Date');
        x!.pinned = true;
      }
      if (b.minutes != null && Number(b.minutes) !== x!.minutes) {
        x!.minutes = num(b.minutes, 5, 600, 'Minutes');
        x!.pinned = true;
      }
      if (b.pinned != null) x!.pinned = !!b.pinned;
    }),
  );

  api.post(
    '/sessions',
    mutate('Added a session', (st, { req }) => {
      const b = req.body ?? {};
      const materialId = str(b.materialId, 'Material');
      const m = st.materials.find((x) => x.id === materialId);
      const d = /^(auto|practice):/.test(materialId) ? st.deadlines.find((x) => x.id === materialId.split(':')[1]) : undefined;
      need(m || d, 'Material not found');
      const sess: Session = {
        id: randomId('u_'),
        date: date(b.date, 'Date'),
        materialId,
        deadlineId: m?.deadlineId ?? d?.id,
        courseId: (m ?? d)!.courseId,
        kind: b.kind === 'review' ? 'review' : 'new',
        minutes: num(b.minutes ?? 30, 5, 600, 'Minutes'),
        pinned: true,
        status: 'planned',
        reasons: [],
      };
      st.sessions.push(sess);
    }),
  );

  api.delete(
    '/sessions/:id',
    mutate('Removed a session', (st, { req }) => {
      const x = st.sessions.find((y) => y.id === req.params.id);
      need(x, 'Session not found');
      if (x!.id.startsWith('u_')) st.sessions = st.sessions.filter((y) => y.id !== x!.id);
      else {
        x!.status = 'skipped';
        x!.pinned = false;
      }
    }),
  );

  api.post('/undo', async (req, res) => {
    const userId = req.user!.id;
    try {
      const state = await withUserLock(db, userId, async (tx) => {
        const before = await loadState(tx, userId);
        const after = structuredClone(before);
        need(undo(after), 'Nothing to undo');
        await saveState(tx, userId, before, after);
        return after;
      });
      res.json(await publicState(state, req.user!));
    } catch (e) {
      fail(res, e);
    }
  });

  api.post(
    '/inbox/:id',
    mutate(null, (st, { req }) => {
      const e = st.inbox.find((x) => x.id === req.params.id);
      need(e && !e.resolved, 'Inbox item not found');
      const entry = e!;
      const action = String(req.body?.action ?? '');
      const deadline = entry.refId ? st.deadlines.find((d) => d.id === entry.refId) : undefined;
      const material = entry.refId ? st.materials.find((m) => m.id === entry.refId) : undefined;
      if (entry.change === 'new') {
        if (action === 'accept') {
          const o = req.body?.overrides ?? {};
          const item = { ...entry.payload.item };
          if (o.kind) item.kind = oneOf(o.kind, [...KINDS, 'material'] as const, 'Type');
          if (o.dueAt) item.dueAt = date(o.dueAt, 'Date');
          item.confidence = 1;
          if (item.kind === 'material') delete item.dueAt;
          need(item.kind === 'material' || item.dueAt, 'Pick a date first');
          const created = createFromItem(st, { id: entry.sourceId }, item);
          if (entry.payload.materials && 'dueAt' in created) {
            for (const [i, mm] of (entry.payload.materials as any[]).entries()) {
              st.materials.push({
                id: randomId('m_'),
                courseId: created.courseId,
                deadlineId: created.id,
                title: String(mm.title).slice(0, 300),
                unitType: UNIT_TYPES.includes(mm.unitType) ? mm.unitType : 'topics',
                quantity: Math.max(0, Math.min(10000, Number(mm.quantity) || 1)),
                difficulty: [1, 2, 3].includes(mm.difficulty) ? mm.difficulty : 2,
                timeAdjust: 1,
                order: i,
                done: false,
                userEdited: [],
              });
            }
          }
        } else {
          need(action === 'ignore', 'Use accept or ignore');
          entry.payload = { ...entry.payload, ignored: true };
        }
      } else if (entry.change === 'conflict') {
        const field = entry.payload.field as 'title' | 'dueAt' | 'kind';
        if (action === 'theirs' && deadline) {
          (deadline as any)[field] = entry.payload.theirs;
          deadline.userEdited = deadline.userEdited.filter((f) => f !== field);
        } else need(action === 'mine' || !deadline, 'Use mine or theirs');
      } else if (entry.change === 'removed') {
        if (action === 'remove') {
          if (deadline) {
            st.deadlines = st.deadlines.filter((d) => d.id !== deadline.id);
            for (const m of st.materials) if (m.deadlineId === deadline.id) delete m.deadlineId;
          }
          if (material) st.materials = st.materials.filter((m) => m.id !== material.id);
        } else {
          need(action === 'keep', 'Use remove or keep');
          const x = deadline ?? material;
          if (x) delete x.origin;
        }
      }
      entry.resolved = true;
    }),
  );

  // ---------- Results (post-exam debrief + grade) ----------
  api.post(
    '/results',
    mutate('Saved an exam result', (st, { req }) => {
      const b = req.body ?? {};
      const d = st.deadlines.find((x) => x.id === b.deadlineId);
      need(d, 'Exam not found');
      const ratings = topicRatings(b.topicRatings, st, d!.id);
      const effort = effortFor(st, d!.id, ratings);
      const existing = st.results.find((r) => r.deadlineId === d!.id);
      const r: ExamResult = existing ?? {
        id: randomId('r_'),
        deadlineId: d!.id,
        courseId: d!.courseId,
        title: d!.title,
        kind: d!.kind,
        dueAt: d!.dueAt,
        target: targetShare(st, d!.courseId),
        minutesInApp: effort.minutesInApp,
        minutesOutside: 0,
        baseEstimateMin: effort.baseEstimateMin,
        baseSource: effort.baseSource,
        topicRatings: {},
        excluded: false,
        reminderCount: 0,
        recordedAt: new Date().toISOString(),
      };
      applyResultFields(r, b, ratings);
      r.minutesInApp = effort.minutesInApp;
      r.baseEstimateMin = effort.baseEstimateMin;
      r.baseSource = effort.baseSource;
      if (!existing) st.results.push(r);
      for (const [id, rating] of Object.entries(ratings)) {
        const m = st.materials.find((x) => x.id === id);
        if (m && rating !== 'not_on_exam') m.weak = rating === 'weak';
      }
      if (d!.dueAt <= todayFor(st)) d!.status = 'done';
    }),
  );

  api.put(
    '/results/:id',
    mutate('Edited an exam result', (st, { req }) => {
      const r = st.results.find((x) => x.id === req.params.id);
      need(r, 'Result not found');
      const ratings = req.body?.topicRatings ? topicRatings(req.body.topicRatings, st, r!.deadlineId) : undefined;
      applyResultFields(r!, req.body ?? {}, ratings);
      if (ratings) {
        const effort = effortFor(st, r!.deadlineId, ratings);
        r!.minutesInApp = effort.minutesInApp;
        r!.baseEstimateMin = effort.baseEstimateMin || r!.baseEstimateMin;
      }
    }),
  );

  api.delete(
    '/results/:id',
    mutate('Deleted an exam result', (st, { req }) => {
      st.results = st.results.filter((r) => r.id !== req.params.id);
    }),
  );

  // ---------- Sources ----------
  api.post('/sources', async (req, res) => {
    try {
      let newId = '';
      await applyMutation(req, null, (st) => {
        const b = req.body ?? {};
        const type = oneOf(b.type, ['demo', 'ics'] as const, 'Type');
        need(st.sources.length < 20, 'Too many connected apps');
        if (type === 'demo') need(!st.sources.some((x) => x.type === 'demo'), 'Demo data is already added');
        if (type === 'ics') need(typeof b.url === 'string' && /^(https?|webcal):\/\//i.test(b.url.trim()) && b.url.length < 2000, 'Paste a feed URL starting with https:// or webcal://');
        const source: Source = {
          id: randomId('src_'),
          type,
          name: type === 'demo' ? 'Demo school' : str(b.name || 'Calendar feed', 'Name', 60),
          config: type === 'demo' ? { anchor: todayFor(st) } : { url: b.url.trim(), feedType: b.feedType === 'calendar' ? 'calendar' : 'lms' },
          status: 'never',
          intervalMin: DEFAULT_INTERVAL[type],
          errorCount: 0,
        };
        newId = source.id;
        st.sources.push(source);
      });
      await syncSource(db, req.user!.id, newId);
      await respondState(req, res);
    } catch (e) {
      fail(res, e);
    }
  });

  api.put(
    '/sources/:id',
    mutate(null, (st, { req }) => {
      const src = st.sources.find((x) => x.id === req.params.id);
      need(src, 'Source not found');
      const b = req.body ?? {};
      if (b.name) src!.name = str(b.name, 'Name', 60);
      if (b.intervalMin != null) src!.intervalMin = num(b.intervalMin, 15, 1440, 'Interval');
      if (Array.isArray(b.calendarIds)) src!.config.calendarIds = b.calendarIds.slice(0, 20).map(String);
      if (b.url && src!.type === 'ics') {
        need(/^(https?|webcal):\/\//i.test(String(b.url).trim()), 'Feed URL must start with https:// or webcal://');
        src!.config.url = String(b.url).trim();
        delete src!.etag;
        delete src!.lastModified;
      }
    }),
  );

  api.delete(
    '/sources/:id',
    mutate('Removed a connected app', async (st, { req, tx, userId }) => {
      const id = String(req.params.id);
      const src = st.sources.find((x) => x.id === id);
      need(src, 'Source not found');
      removeSource(st, id, !!req.query.keepItems);
      if (src!.type.startsWith('google') && !st.sources.some((x) => x.type.startsWith('google'))) await disconnectGoogle(tx, userId);
    }),
  );

  api.post('/sources/:id/sync', async (req, res) => {
    try {
      await syncSource(db, req.user!.id, String(req.params.id));
      await respondState(req, res);
    } catch (e) {
      fail(res, e);
    }
  });

  api.post('/sync', async (req, res) => {
    try {
      await syncAllForUser(db, req.user!.id);
      await respondState(req, res);
    } catch (e) {
      fail(res, e);
    }
  });

  // ---------- Google ----------
  api.get('/google/calendars', async (req, res) => {
    try {
      const st = await loadState(db, req.user!.id);
      const google = await googleApi(db, req.user!.id);
      res.json(await listCalendars(google, st.sources.flatMap((x) => x.config.courseCalendarIds ?? [])));
    } catch (e) {
      res.status(400).json({ error: (e as Error).message });
    }
  });

  api.post(
    '/google/disconnect',
    mutate('Disconnected Google', async (st, { tx, userId, req }) => {
      for (const src of st.sources.filter((x) => x.type.startsWith('google'))) removeSource(st, src.id, !!req.body?.keepItems);
      await disconnectGoogle(tx, userId);
    }),
  );

  // ---------- Reminders ----------
  api.post('/push/subscribe', async (req, res) => {
    try {
      const b = req.body ?? {};
      const endpoint = str(b.endpoint, 'endpoint', 1000);
      need(/^https:\/\//.test(endpoint), 'Invalid push endpoint');
      const p256dh = str(b.keys?.p256dh, 'p256dh', 200);
      const authKey = str(b.keys?.auth, 'auth', 100);
      const row = { id: randomId('push_'), userId: req.user!.id, endpoint, p256dh, auth: authKey, userAgent: optStr(req.get('user-agent'), 200) };
      await db
        .insert(s.pushSubscription)
        .values(row)
        .onConflictDoUpdate({ target: s.pushSubscription.endpoint, set: { userId: row.userId, p256dh, auth: authKey, failureCount: 0 } });
      res.json({ ok: true });
    } catch (e) {
      fail(res, e);
    }
  });

  api.post('/push/unsubscribe', async (req, res) => {
    const endpoint = String(req.body?.endpoint ?? '');
    await db.delete(s.pushSubscription).where(and(eq(s.pushSubscription.userId, req.user!.id), eq(s.pushSubscription.endpoint, endpoint)));
    res.json({ ok: true });
  });

  api.post('/push/test', async (req, res) => {
    try {
      const st = await loadState(db, req.user!.id);
      const d = buildDigest(st);
      const sent = await sendPush(db, req.user!.id, { title: `📚 ${d.subject}`, body: d.short, url: '/', tag: 'test' });
      need(sent > 0, 'No device is subscribed yet. Turn on notifications on this device first.');
      res.json({ ok: true, sent });
    } catch (e) {
      fail(res, e);
    }
  });

  api.get('/digest', async (req, res) => {
    res.json(buildDigest(await loadState(db, req.user!.id)));
  });

  api.get('/export.ics', async (req, res) => {
    res.setHeader('Content-Type', 'text/calendar; charset=utf-8');
    res.setHeader('Content-Disposition', 'attachment; filename="study-plan.ics"');
    res.send(exportIcs(await loadState(db, req.user!.id)));
  });

  // ---------- Account ----------
  api.get('/account/export', async (req, res) => {
    const st = await loadState(db, req.user!.id);
    const { snapshots: _s, ...data } = st;
    res.setHeader('Content-Disposition', `attachment; filename="${APP_NAME.toLowerCase()}-data.json"`);
    res.json({ exportedAt: new Date().toISOString(), user: req.user, ...data });
  });

  api.delete('/account', async (req, res) => {
    try {
      need(req.body?.confirm === 'DELETE', 'Type DELETE to confirm');
      await disconnectGoogle(db, req.user!.id);
      await db.delete(s.user).where(eq(s.user.id, req.user!.id));
      res.json({ ok: true });
    } catch (e) {
      fail(res, e);
    }
  });

  registerAiRoutes(api, { db, applyMutation, mutate, fail, respondState, publicState, aiAllowedFor });

  app.use('/api', api);

  // ---------- Google OAuth (top-level navigations, cookie-authenticated) ----------
  app.get('/oauth/google/start', requireUser, (req, res) => {
    try {
      res.redirect(connectUrl(req.user!.id));
    } catch (e) {
      res.redirect(`/?view=sources&error=${encodeURIComponent((e as Error).message)}`);
    }
  });

  app.get('/oauth/google/callback', requireUser, async (req, res) => {
    const { code, state, error } = req.query as Record<string, string>;
    if (error) {
      const hint =
        error === 'access_denied'
          ? 'Google sign-in was cancelled or blocked. School accounts for students under 18 need the school admin to allow this app; until then, use your school calendar feed (ICS) or add exams yourself.'
          : `Google returned: ${error}`;
      return res.redirect(`/?view=sources&error=${encodeURIComponent(hint)}`);
    }
    const userId = req.user!.id;
    try {
      const { missingScopes } = await finishConnect(db, userId, String(code), String(state));
      await withUserLock(db, userId, async (tx) => {
        const before = await loadState(tx, userId);
        const after = structuredClone(before);
        for (const type of ['google-classroom', 'google-calendar'] as const) {
          const existing = after.sources.find((x) => x.type === type);
          if (existing) {
            existing.needsReconnect = false;
            existing.errorCount = 0;
            continue;
          }
          after.sources.push({
            id: randomId('src_'),
            type,
            name: type === 'google-classroom' ? 'Google Classroom' : 'Google Calendar',
            config: type === 'google-calendar' ? { calendarIds: ['primary'] } : {},
            status: 'never',
            intervalMin: DEFAULT_INTERVAL[type],
            errorCount: 0,
          });
        }
        await saveState(tx, userId, before, after);
      });
      void syncAllForUser(db, userId).catch((e) => console.error('[google] first sync failed:', e.message));
      const note = missingScopes.length ? '&warning=' + encodeURIComponent('Some permissions were not granted, so part of your Classroom or Calendar data may be missing.') : '';
      res.redirect(`/?view=sources&connected=google${note}`);
    } catch (e) {
      res.redirect(`/?view=sources&error=${encodeURIComponent((e as Error).message)}`);
    }
  });

  // ---------- Frontend ----------
  const dist = path.resolve('dist');
  if (fs.existsSync(dist)) {
    app.use(
      express.static(dist, {
        index: false,
        setHeaders: (res, file) => {
          if (file.includes(`${path.sep}assets${path.sep}`)) res.setHeader('Cache-Control', 'public, max-age=31536000, immutable');
          else res.setHeader('Cache-Control', 'no-cache');
        },
      }),
    );
    app.use((req, res, next) => {
      if (req.method !== 'GET' || req.path.startsWith('/api') || req.path.startsWith('/oauth')) return next();
      res.setHeader('Cache-Control', 'no-cache');
      res.sendFile(path.join(dist, 'index.html'));
    });
  }

  app.use((err: unknown, _req: Request, res: Response, _next: NextFunction) => {
    console.error('[server]', err);
    if (!res.headersSent) res.status(500).json({ error: 'Something went wrong. Please try again.' });
  });

  return app;
}

/** Whole years since the birth month; counted conservatively (the birthday month itself doesn't count). */
export function ageInYears(birthMonth: string, now = new Date()): number {
  const y = Number(birthMonth.slice(0, 4));
  const m = Number(birthMonth.slice(5, 7));
  const ny = now.getUTCFullYear();
  const nm = now.getUTCMonth() + 1;
  return ny - y - (nm <= m ? 1 : 0);
}

/** AI features are for adults unless the operator has added safeguards for minors (AI_ALLOW_MINORS). */
export function aiAllowedFor(state: Pick<UserState, 'settings'>): boolean {
  if (!features.ai()) return false;
  if (env.anthropic.allowMinors) return true;
  return !!state.settings.birthMonth && ageInYears(state.settings.birthMonth) >= 18;
}

const isAdmin = (u: PublicUser) => u.emailVerified && env.adminEmails.includes(u.email.toLowerCase());

function validTz(tz: unknown): boolean {
  if (typeof tz !== 'string' || tz.length > 64) return false;
  try {
    new Intl.DateTimeFormat('en', { timeZone: tz });
    return true;
  } catch {
    return false;
  }
}

function gradeValue(v: any): GradeValue {
  need(v && typeof v === 'object', 'Grade is required');
  const g: GradeValue = { scale: oneOf(v.scale, GRADE_SCALES.map((x) => x.id), 'Grade scale'), value: typeof v.value === 'number' ? v.value : String(v.value ?? '').slice(0, 20) };
  if (v.max != null) g.max = num(v.max, 1, 100000, 'Maximum');
  normalizeGrade(g); // throws GradeError when out of range
  return g;
}

function topicRatings(v: any, st: UserState, deadlineId: string): Record<string, TopicRating> {
  const out: Record<string, TopicRating> = {};
  if (!v || typeof v !== 'object') return out;
  const ids = new Set(st.materials.filter((m) => m.deadlineId === deadlineId).map((m) => m.id));
  for (const [id, r] of Object.entries(v)) if (ids.has(id) && RATINGS.includes(r as TopicRating)) out[id] = r as TopicRating;
  return out;
}

function applyResultFields(r: ExamResult, b: any, ratings?: Record<string, TopicRating>) {
  if (b.grade === null) {
    delete r.grade;
    delete r.score;
    delete r.gradedAt;
  } else if (b.grade !== undefined) {
    r.grade = gradeValue(b.grade);
    r.score = normalizeGrade(r.grade);
    r.gradedAt = new Date().toISOString();
  }
  if (b.minutesOutside != null) r.minutesOutside = num(b.minutesOutside, 0, 6000, 'Outside study time');
  if (b.feltDifficulty != null) r.feltDifficulty = num(b.feltDifficulty, -1, 1, 'How the exam felt');
  if (b.excluded != null) r.excluded = !!b.excluded;
  if (b.note !== undefined) r.note = optStr(b.note, 500);
  if (b.gradeDueAt === null) delete r.gradeDueAt;
  else if (b.gradeDueAt) r.gradeDueAt = date(b.gradeDueAt, 'Grade date');
  if (ratings) r.topicRatings = ratings;
}

function removeSource(st: UserState, id: string, keepItems: boolean) {
  st.sources = st.sources.filter((x) => x.id !== id);
  st.busy = st.busy.filter((b) => b.sourceId !== id);
  st.inbox = st.inbox.filter((e) => e.sourceId !== id);
  if (!keepItems) {
    const gone = new Set(st.deadlines.filter((d) => d.origin?.sourceId === id).map((d) => d.id));
    st.deadlines = st.deadlines.filter((d) => !gone.has(d.id));
    st.materials = st.materials.filter((m) => m.origin?.sourceId !== id);
    for (const m of st.materials) if (m.deadlineId && gone.has(m.deadlineId)) delete m.deadlineId;
  } else {
    for (const d of st.deadlines) if (d.origin?.sourceId === id) delete d.origin;
    for (const m of st.materials) if (m.origin?.sourceId === id) delete m.origin;
  }
}
