import express, { type Request, type Response } from 'express';
import fs from 'node:fs';
import path from 'node:path';
import { addDays } from '../shared/dates';
import type { DB, Deadline, Difficulty, Material, PublicState, Session, Source, UnitType } from '../shared/types';
import { updateCalibration } from '../planner/estimate';
import { parseMaterialLine } from './classify';
import { authUrl, googleConnected, handleCallback, listCalendars } from './connectors/google';
import { buildDigest, sendEmailDigest, startDigestScheduler } from './digest';
import { exportIcs } from './ics-export';
import { replan, snapshot, todayFor, undo } from './plan-service';
import { defaultDB, deleteSecretFile, load, replace, save, uid } from './store';
import { createFromItem, DEFAULT_INTERVAL, findOrCreateCourse, startSyncScheduler, syncAll, syncSource } from './sync';

const PORT = Number(process.env.PORT ?? 3000);
const PUBLIC_URL = (process.env.PUBLIC_URL ?? `http://localhost:${PORT}`).replace(/\/$/, '');
const REDIRECT_URI = `${PUBLIC_URL}/oauth/google/callback`;

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const UNIT_TYPES: UnitType[] = ['pages', 'slides', 'problems', 'videoMinutes', 'topics', 'minutes'];

class BadRequest extends Error {}
const need = (cond: unknown, msg: string): void => {
  if (!cond) throw new BadRequest(msg);
};
const date = (v: unknown, name: string) => {
  need(typeof v === 'string' && DATE_RE.test(v), `${name} must be a date (YYYY-MM-DD)`);
  return v as string;
};
const num = (v: unknown, lo: number, hi: number, name: string) => {
  const n = Number(v);
  need(Number.isFinite(n) && n >= lo && n <= hi, `${name} must be between ${lo} and ${hi}`);
  return n;
};
const str = (v: unknown, name: string, max = 300) => {
  need(typeof v === 'string' && v.trim().length > 0, `${name} is required`);
  return (v as string).trim().slice(0, max);
};

export function publicState(db: DB): PublicState {
  const today = todayFor(db);
  const { snapshots, settings, ...rest } = db;
  const { email, googleClient, ...s } = settings;
  return {
    ...rest,
    sessions: rest.sessions.filter((x) => x.date >= addDays(today, -60)),
    inbox: rest.inbox.filter((e) => !e.resolved),
    digestLog: rest.digestLog.slice(-7),
    settings: {
      ...s,
      email: email ? { host: email.host, port: email.port, secure: email.secure, user: email.user, from: email.from, to: email.to, hasPass: !!email.pass } : undefined,
      googleClient: googleClient ? { clientId: googleClient.clientId, hasSecret: !!googleClient.clientSecret } : undefined,
    },
    canUndo: snapshots.at(-1)?.label ?? null,
    googleConnected: googleConnected(),
    redirectUri: REDIRECT_URI,
    today,
  };
}

type Handler = (db: DB, req: Request) => void | Promise<void>;

/** Wraps a user change: snapshot for undo, apply, re-plan, save, return the new state. */
const mutate = (label: string | null, fn: Handler) => async (req: Request, res: Response) => {
  const db = load();
  if (label) snapshot(db, label);
  try {
    await fn(db, req);
    replan(db);
    save();
    res.json(publicState(db));
  } catch (e) {
    if (label) db.snapshots.pop();
    res.status(e instanceof BadRequest ? 400 : 500).json({ error: (e as Error).message });
  }
};

const app = express();
app.use(express.json({ limit: '1mb' }));

app.get('/api/state', (_req, res) => res.json(publicState(load())));

app.post(
  '/api/onboarding',
  mutate(null, (db) => {
    db.settings.onboarded = true;
  }),
);

app.put(
  '/api/settings',
  mutate(null, (db, req) => {
    const b = req.body ?? {};
    const s = db.settings;
    if (b.timezone != null) {
      try {
        new Intl.DateTimeFormat('en', { timeZone: b.timezone });
      } catch {
        throw new BadRequest('Unknown timezone');
      }
      s.timezone = b.timezone;
    }
    if (b.digestTime != null) {
      need(/^\d{2}:\d{2}$/.test(b.digestTime), 'digestTime must be HH:MM');
      s.digestTime = b.digestTime;
    }
    if (b.slack != null) s.slack = num(b.slack, 0.5, 1, 'slack');
    if (b.minChunk != null) s.minChunk = num(b.minChunk, 10, 60, 'minChunk');
    if (b.maxSession != null) s.maxSession = num(b.maxSession, 20, 180, 'maxSession');
    if (b.maxSubjects != null) s.maxSubjects = num(b.maxSubjects, 1, 6, 'maxSubjects');
    need(s.minChunk <= s.maxSession, 'Shortest session must not exceed the longest');
    if (b.email === null) delete s.email;
    else if (b.email) {
      const e = b.email;
      s.email = {
        host: str(e.host, 'SMTP host'),
        port: num(e.port ?? 587, 1, 65535, 'port'),
        secure: !!e.secure,
        user: String(e.user ?? ''),
        pass: e.pass ? String(e.pass) : s.email?.pass ?? '',
        from: String(e.from ?? e.user ?? ''),
        to: str(e.to, 'Recipient'),
      };
    }
  }),
);

app.put(
  '/api/availability',
  mutate('Changed weekly availability', (db, req) => {
    const b = req.body ?? {};
    need(Array.isArray(b.minutesByWeekday) && b.minutesByWeekday.length === 7, 'minutesByWeekday needs 7 values');
    db.availability.minutesByWeekday = b.minutesByWeekday.map((m: unknown, i: number) => num(m, 0, 960, `Day ${i}`));
    if (b.studyWindow === null) delete db.availability.studyWindow;
    else if (b.studyWindow) {
      need(/^\d{2}:\d{2}$/.test(b.studyWindow.start) && /^\d{2}:\d{2}$/.test(b.studyWindow.end) && b.studyWindow.start < b.studyWindow.end, 'Study window must be a valid time range');
      db.availability.studyWindow = { start: b.studyWindow.start, end: b.studyWindow.end };
    }
  }),
);

app.post(
  '/api/blocks',
  mutate('Blocked dates', (db, req) => {
    const start = date(req.body?.start, 'start');
    const end = req.body?.end ? date(req.body.end, 'end') : start;
    need(end >= start, 'End must be on or after start');
    db.blocks.push({ id: uid('b_'), start, end, reason: req.body?.reason ? String(req.body.reason).slice(0, 100) : undefined });
  }),
);

app.delete(
  '/api/blocks/:id',
  mutate('Removed a blocked period', (db, req) => {
    db.blocks = db.blocks.filter((b) => b.id !== req.params.id);
  }),
);

app.post(
  '/api/today/off',
  mutate("Can't study today", (db) => {
    const today = todayFor(db);
    db.blocks.push({ id: uid('b_'), start: today, end: today, reason: "Can't study today" });
    for (const s of db.sessions) if (s.date === today && s.status === 'planned') s.status = 'skipped';
  }),
);

app.post(
  '/api/courses',
  mutate('Added a course', (db, req) => {
    findOrCreateCourse(db, str(req.body?.name, 'Course name', 80));
  }),
);

app.put(
  '/api/courses/:id',
  mutate('Edited a course', (db, req) => {
    const c = db.courses.find((x) => x.id === req.params.id);
    need(c, 'Course not found');
    if (req.body?.name) c!.name = str(req.body.name, 'Course name', 80);
    if (req.body?.color && /^#[0-9a-f]{6}$/i.test(req.body.color)) c!.color = req.body.color;
  }),
);

app.delete(
  '/api/courses/:id',
  mutate('Deleted a course', (db, req) => {
    const id = req.params.id;
    db.courses = db.courses.filter((c) => c.id !== id);
    db.deadlines = db.deadlines.filter((d) => d.courseId !== id);
    db.materials = db.materials.filter((m) => m.courseId !== id);
    db.sessions = db.sessions.filter((s) => s.courseId !== id || s.status !== 'planned');
  }),
);

const courseFrom = (db: DB, b: any) => {
  if (b.courseId) {
    const c = db.courses.find((x) => x.id === b.courseId);
    need(c, 'Course not found');
    return c!;
  }
  return findOrCreateCourse(db, str(b.courseName, 'Course', 80));
};

app.post(
  '/api/deadlines',
  mutate('Added an exam or deadline', (db, req) => {
    const b = req.body ?? {};
    need(['exam', 'quiz', 'assignment'].includes(b.kind), 'Kind must be exam, quiz or assignment');
    const course = courseFrom(db, b);
    const d: Deadline = { id: uid('d_'), courseId: course.id, kind: b.kind, title: str(b.title, 'Title'), dueAt: date(b.dueAt, 'Due date'), status: 'active', userEdited: [], confidence: 1 };
    db.deadlines.push(d);
  }),
);

app.put(
  '/api/deadlines/:id',
  mutate('Edited a deadline', (db, req) => {
    const d = db.deadlines.find((x) => x.id === req.params.id);
    need(d, 'Deadline not found');
    const b = req.body ?? {};
    const edit = (field: 'title' | 'dueAt' | 'kind', value: any) => {
      if (d![field] === value) return;
      (d as any)[field] = value;
      if (d!.origin && !d!.userEdited.includes(field)) d!.userEdited.push(field);
    };
    if (b.title != null) edit('title', str(b.title, 'Title'));
    if (b.dueAt != null) edit('dueAt', date(b.dueAt, 'Due date'));
    if (b.kind != null) {
      need(['exam', 'quiz', 'assignment'].includes(b.kind), 'Bad kind');
      edit('kind', b.kind);
    }
    if (b.status != null) {
      need(b.status === 'active' || b.status === 'done', 'Bad status');
      d!.status = b.status;
    }
    if (b.courseId) d!.courseId = courseFrom(db, b).id;
  }),
);

app.delete(
  '/api/deadlines/:id',
  mutate('Deleted a deadline', (db, req) => {
    const id = req.params.id;
    db.deadlines = db.deadlines.filter((d) => d.id !== id);
    if (req.query.keepMaterials) {
      for (const m of db.materials) if (m.deadlineId === id) delete m.deadlineId;
    } else {
      db.materials = db.materials.filter((m) => m.deadlineId !== id);
    }
    db.sessions = db.sessions.filter((s) => s.deadlineId !== id || s.status !== 'planned');
  }),
);

const materialFields = (b: any, m: Partial<Material>) => {
  if (b.title != null) m.title = str(b.title, 'Title');
  if (b.unitType != null) {
    need(UNIT_TYPES.includes(b.unitType), 'Unknown unit type');
    m.unitType = b.unitType;
  }
  if (b.quantity != null) m.quantity = num(b.quantity, 0, 100000, 'Quantity');
  if (b.difficulty != null) m.difficulty = num(b.difficulty, 1, 3, 'Difficulty') as Difficulty;
  if (b.estimateOverride === null || b.estimateOverride === '') delete m.estimateOverride;
  else if (b.estimateOverride != null) m.estimateOverride = num(b.estimateOverride, 0, 100000, 'Estimate');
  if (b.availableFrom === null || b.availableFrom === '') delete m.availableFrom;
  else if (b.availableFrom != null) m.availableFrom = date(b.availableFrom, 'Available from');
  if (b.done != null) m.done = !!b.done;
};

app.post(
  '/api/materials',
  mutate('Added material', (db, req) => {
    const b = req.body ?? {};
    const course = courseFrom(db, b);
    const deadlineId = b.deadlineId || undefined;
    if (deadlineId) need(db.deadlines.some((d) => d.id === deadlineId), 'Deadline not found');
    const order0 = db.materials.filter((m) => m.courseId === course.id).length;
    type Row = ReturnType<typeof parseMaterialLine>;
    const rows: Row[] = typeof b.lines === 'string' ? (b.lines as string).split(/\r?\n/).map(parseMaterialLine).filter((r): r is NonNullable<Row> => !!r) : [null];
    need(rows.length > 0, 'Paste at least one line');
    rows.forEach((row, i) => {
      const m: Material = { id: uid('m_'), courseId: course.id, deadlineId, title: '', unitType: 'topics', quantity: 1, difficulty: 2, order: order0 + i, done: false, userEdited: [] };
      materialFields(row ?? b, m);
      if (row && b.difficulty) m.difficulty = num(b.difficulty, 1, 3, 'Difficulty') as Difficulty;
      need(m.title, 'Title is required');
      db.materials.push(m);
    });
  }),
);

app.put(
  '/api/materials/:id',
  mutate('Edited material', (db, req) => {
    const m = db.materials.find((x) => x.id === req.params.id);
    need(m, 'Material not found');
    const before = { ...m! };
    materialFields(req.body ?? {}, m!);
    if ('deadlineId' in (req.body ?? {})) {
      const id = req.body.deadlineId || undefined;
      if (id) need(db.deadlines.some((d) => d.id === id), 'Deadline not found');
      m!.deadlineId = id;
    }
    if (m!.origin) for (const f of ['title', 'unitType', 'quantity', 'deadlineId'] as const) if (before[f] !== m![f] && !m!.userEdited.includes(f)) m!.userEdited.push(f);
  }),
);

app.delete(
  '/api/materials/:id',
  mutate('Deleted material', (db, req) => {
    db.materials = db.materials.filter((m) => m.id !== req.params.id);
    db.sessions = db.sessions.filter((s) => s.materialId !== req.params.id || s.status !== 'planned');
  }),
);

app.put(
  '/api/sessions/:id',
  mutate('Edited a session', (db, req) => {
    const s = db.sessions.find((x) => x.id === req.params.id);
    need(s, 'Session not found');
    const b = req.body ?? {};
    if (b.status != null) {
      need(['planned', 'done', 'partial', 'skipped'].includes(b.status), 'Bad status');
      s!.status = b.status;
      if (b.status === 'planned') delete s!.actualMinutes;
    }
    if (b.actualMinutes != null && b.actualMinutes !== '') {
      s!.actualMinutes = num(b.actualMinutes, 0, 600, 'Actual minutes');
      if (s!.status === 'done' && s!.kind === 'new') {
        const i = db.courses.findIndex((c) => c.id === s!.courseId);
        if (i >= 0) db.courses[i] = updateCalibration(db.courses[i], s!.minutes, s!.actualMinutes);
      }
    }
    if (b.date != null && b.date !== s!.date) {
      s!.date = date(b.date, 'Date');
      s!.pinned = true;
    }
    if (b.minutes != null && Number(b.minutes) !== s!.minutes) {
      s!.minutes = num(b.minutes, 5, 600, 'Minutes');
      s!.pinned = true;
    }
    if (b.pinned != null) s!.pinned = !!b.pinned;
  }),
);

app.post(
  '/api/sessions',
  mutate('Added a session', (db, req) => {
    const b = req.body ?? {};
    const materialId = str(b.materialId, 'Material');
    const m = db.materials.find((x) => x.id === materialId);
    const d = materialId.startsWith('auto:') ? db.deadlines.find((x) => x.id === materialId.slice(5)) : undefined;
    need(m || d, 'Material not found');
    const s: Session = {
      id: uid('u_'),
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
    db.sessions.push(s);
  }),
);

app.delete(
  '/api/sessions/:id',
  mutate('Removed a session', (db, req) => {
    const s = db.sessions.find((x) => x.id === req.params.id);
    need(s, 'Session not found');
    if (s!.id.startsWith('u_')) db.sessions = db.sessions.filter((x) => x.id !== s!.id);
    else {
      s!.status = 'skipped';
      s!.pinned = false;
    }
  }),
);

app.post('/api/undo', (_req, res) => {
  const db = load();
  if (!undo(db)) return res.status(400).json({ error: 'Nothing to undo' });
  save();
  res.json(publicState(db));
});

app.post(
  '/api/inbox/:id',
  mutate(null, (db, req) => {
    const e = db.inbox.find((x) => x.id === req.params.id);
    need(e && !e.resolved, 'Inbox item not found');
    const action = String(req.body?.action ?? '');
    const entry = e!;
    const deadline = entry.refId ? db.deadlines.find((d) => d.id === entry.refId) : undefined;
    const material = entry.refId ? db.materials.find((m) => m.id === entry.refId) : undefined;
    if (entry.change === 'new') {
      if (action === 'accept') {
        const item = { ...entry.payload.item, ...(req.body?.overrides ?? {}) };
        if (item.kind === 'material') delete item.dueAt;
        createFromItem(db, { id: entry.sourceId }, item);
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
          db.deadlines = db.deadlines.filter((d) => d.id !== deadline.id);
          for (const m of db.materials) if (m.deadlineId === deadline.id) delete m.deadlineId;
        }
        if (material) db.materials = db.materials.filter((m) => m.id !== material.id);
      } else {
        need(action === 'keep', 'Use remove or keep');
        const x = deadline ?? material;
        if (x) delete x.origin; // stop tracking: it's now a manual item
      }
    }
    entry.resolved = true;
  }),
);

app.post(
  '/api/sources',
  mutate(null, async (db, req) => {
    const b = req.body ?? {};
    need(['demo', 'ics'].includes(b.type), 'Use the Google connect button for Google sources');
    const today = todayFor(db);
    if (b.type === 'demo') need(!db.sources.some((s) => s.type === 'demo'), 'Demo data is already added');
    if (b.type === 'ics') {
      need(typeof b.url === 'string' && /^(https?|webcal):\/\//i.test(b.url.trim()), 'Paste a feed URL starting with https:// or webcal://');
    }
    const source: Source = {
      id: uid('src_'),
      type: b.type,
      name: b.type === 'demo' ? 'Demo school' : str(b.name || 'Calendar feed', 'Name', 60),
      config: b.type === 'demo' ? { anchor: today } : { url: b.url.trim(), feedType: b.feedType === 'calendar' ? 'calendar' : 'lms' },
      status: 'never',
      intervalMin: DEFAULT_INTERVAL[b.type as Source['type']],
      errorCount: 0,
    };
    db.sources.push(source);
    await syncSource(source.id);
  }),
);

app.put(
  '/api/sources/:id',
  mutate(null, (db, req) => {
    const s = db.sources.find((x) => x.id === req.params.id);
    need(s, 'Source not found');
    const b = req.body ?? {};
    if (b.name) s!.name = str(b.name, 'Name', 60);
    if (b.intervalMin != null) s!.intervalMin = num(b.intervalMin, 5, 1440, 'Interval');
    if (Array.isArray(b.calendarIds)) s!.config.calendarIds = b.calendarIds.map(String);
    if (b.url && s!.type === 'ics') {
      s!.config.url = String(b.url).trim();
      delete s!.etag;
      delete s!.lastModified;
    }
  }),
);

app.delete(
  '/api/sources/:id',
  mutate('Removed a connected app', (db, req) => {
    const id = req.params.id;
    const src = db.sources.find((s) => s.id === id);
    need(src, 'Source not found');
    db.sources = db.sources.filter((s) => s.id !== id);
    db.busy = db.busy.filter((b) => b.sourceId !== id);
    db.inbox = db.inbox.filter((e) => e.sourceId !== id);
    if (!req.query.keepItems) {
      const gone = new Set(db.deadlines.filter((d) => d.origin?.sourceId === id).map((d) => d.id));
      db.deadlines = db.deadlines.filter((d) => !gone.has(d.id));
      db.materials = db.materials.filter((m) => m.origin?.sourceId !== id && !(m.deadlineId && gone.has(m.deadlineId) && !m.origin));
      for (const m of db.materials) if (m.deadlineId && gone.has(m.deadlineId)) delete m.deadlineId;
    }
    if (src!.type.startsWith('google') && !db.sources.some((s) => s.type.startsWith('google'))) deleteSecretFile('google-tokens.json');
  }),
);

app.post('/api/sources/:id/sync', async (req, res) => {
  await syncSource(String(req.params.id));
  res.json(publicState(load()));
});

app.post('/api/sync', async (_req, res) => {
  await syncAll();
  res.json(publicState(load()));
});

app.put(
  '/api/google/client',
  mutate(null, (db, req) => {
    const clientId = str(req.body?.clientId, 'Client ID', 300);
    need(/\.apps\.googleusercontent\.com$/.test(clientId), 'Client ID should end with .apps.googleusercontent.com');
    const clientSecret = req.body?.clientSecret ? String(req.body.clientSecret).trim() : db.settings.googleClient?.clientSecret;
    need(clientSecret, 'Client secret is required');
    db.settings.googleClient = { clientId, clientSecret: clientSecret! };
  }),
);

app.get('/oauth/google/start', (_req, res) => {
  try {
    res.redirect(authUrl(load(), REDIRECT_URI));
  } catch (e) {
    res.redirect(`/?view=sources&error=${encodeURIComponent((e as Error).message)}`);
  }
});

app.get('/oauth/google/callback', async (req, res) => {
  const db = load();
  const { code, state, error } = req.query as Record<string, string>;
  if (error) {
    const hint =
      error === 'access_denied'
        ? 'Google sign-in was cancelled or blocked. School accounts often block unverified apps; use your LMS calendar feed (ICS) instead.'
        : `Google returned: ${error}`;
    return res.redirect(`/?view=sources&error=${encodeURIComponent(hint)}`);
  }
  try {
    await handleCallback(db, REDIRECT_URI, code, state);
    for (const type of ['google-classroom', 'google-calendar'] as const) {
      const existing = db.sources.find((s) => s.type === type);
      if (existing) {
        existing.needsReconnect = false;
        existing.errorCount = 0;
        continue;
      }
      db.sources.push({
        id: uid('src_'),
        type,
        name: type === 'google-classroom' ? 'Google Classroom' : 'Google Calendar',
        config: type === 'google-calendar' ? { calendarIds: ['primary'] } : {},
        status: 'never',
        intervalMin: DEFAULT_INTERVAL[type],
        errorCount: 0,
      });
    }
    save();
    void syncAll();
    res.redirect('/?view=sources&connected=google');
  } catch (e) {
    res.redirect(`/?view=sources&error=${encodeURIComponent((e as Error).message)}`);
  }
});

app.get('/api/google/calendars', async (_req, res) => {
  try {
    res.json(await listCalendars(load()));
  } catch (e) {
    res.status(400).json({ error: (e as Error).message });
  }
});

app.get('/api/digest', (_req, res) => res.json(buildDigest(load())));

app.post('/api/digest/email', async (_req, res) => {
  try {
    await sendEmailDigest(load());
    res.json({ ok: true });
  } catch (e) {
    res.status(400).json({ error: (e as Error).message });
  }
});

app.post('/api/digest/browser', (_req, res) => {
  const db = load();
  const today = todayFor(db);
  if (!db.digestLog.some((l) => l.date === today && l.channel === 'browser')) {
    db.digestLog.push({ date: today, channel: 'browser', sentAt: new Date().toISOString() });
    save();
  }
  res.json({ ok: true });
});

app.get('/api/export.ics', (_req, res) => {
  res.setHeader('Content-Type', 'text/calendar; charset=utf-8');
  res.setHeader('Content-Disposition', 'attachment; filename="study-plan.ics"');
  res.send(exportIcs(load()));
});

app.post('/api/reset', (_req, res) => {
  deleteSecretFile('google-tokens.json');
  replace(defaultDB());
  res.json(publicState(load()));
});

// Serve the built frontend.
const dist = path.resolve('dist');
if (fs.existsSync(dist)) {
  app.use(express.static(dist));
  app.use((req, res, next) => {
    if (req.method !== 'GET' || req.path.startsWith('/api') || req.path.startsWith('/oauth')) return next();
    res.sendFile(path.join(dist, 'index.html'));
  });
}

const db = load();
replan(db);
save();
startSyncScheduler();
startDigestScheduler();
app.listen(PORT, () => {
  console.log(`Study Planner running at ${PUBLIC_URL}`);
  if (!fs.existsSync(dist)) console.log('(Frontend not built yet: run `npm start`, or `npm run dev` for development.)');
});
