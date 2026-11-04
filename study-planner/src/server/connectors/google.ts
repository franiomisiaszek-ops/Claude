import crypto from 'node:crypto';
import { addDays, localParts } from '../../shared/dates';
import type { BusyEvent, DB, SourceItem } from '../../shared/types';
import { classify } from '../classify';
import { readSecretFile, save, writeSecretFile } from '../store';
import { ReconnectError, TransientError, type Connector } from './types';

/** Read-only scopes, requested together on one consent screen. */
export const GOOGLE_SCOPES = [
  'https://www.googleapis.com/auth/classroom.courses.readonly',
  'https://www.googleapis.com/auth/classroom.coursework.me.readonly',
  'https://www.googleapis.com/auth/classroom.courseworkmaterials.readonly',
  'https://www.googleapis.com/auth/classroom.topics.readonly',
  'https://www.googleapis.com/auth/calendar.calendarlist.readonly',
  'https://www.googleapis.com/auth/calendar.events.readonly',
];

const TOKEN_FILE = 'google-tokens.json';

interface Tokens {
  access_token: string;
  refresh_token?: string;
  expires_at: number;
  scope?: string;
}

const pending = new Map<string, { verifier: string; createdAt: number }>();
const b64url = (b: Buffer) => b.toString('base64url');

export function googleConnected(): boolean {
  return !!readSecretFile<Tokens>(TOKEN_FILE)?.refresh_token;
}

export function authUrl(db: DB, redirectUri: string): string {
  const client = db.settings.googleClient;
  if (!client) throw new Error('Add your Google OAuth client ID and secret first.');
  const state = b64url(crypto.randomBytes(16));
  const verifier = b64url(crypto.randomBytes(32));
  const challenge = b64url(crypto.createHash('sha256').update(verifier).digest());
  for (const [k, v] of pending) if (Date.now() - v.createdAt > 15 * 60_000) pending.delete(k);
  pending.set(state, { verifier, createdAt: Date.now() });
  const p = new URLSearchParams({
    client_id: client.clientId,
    redirect_uri: redirectUri,
    response_type: 'code',
    scope: GOOGLE_SCOPES.join(' '),
    access_type: 'offline',
    prompt: 'consent',
    include_granted_scopes: 'true',
    state,
    code_challenge: challenge,
    code_challenge_method: 'S256',
  });
  return `https://accounts.google.com/o/oauth2/v2/auth?${p}`;
}

async function tokenRequest(db: DB, body: Record<string, string>): Promise<Tokens> {
  const client = db.settings.googleClient!;
  const res = await fetch('https://oauth2.googleapis.com/token', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ client_id: client.clientId, client_secret: client.clientSecret, ...body }),
  });
  const json = (await res.json().catch(() => ({}))) as any;
  if (!res.ok) {
    if (json.error === 'invalid_grant') throw new ReconnectError('Google access expired or was revoked. Reconnect Google.');
    throw new Error(`Google token error: ${json.error_description ?? json.error ?? res.status}`);
  }
  return {
    access_token: json.access_token,
    refresh_token: json.refresh_token,
    expires_at: Date.now() + (json.expires_in ?? 3600) * 1000,
    scope: json.scope,
  };
}

export async function handleCallback(db: DB, redirectUri: string, code: string, state: string): Promise<void> {
  const p = pending.get(state);
  if (!p) throw new Error('This sign-in link expired. Please try connecting again.');
  pending.delete(state);
  const tokens = await tokenRequest(db, { code, grant_type: 'authorization_code', redirect_uri: redirectUri, code_verifier: p.verifier });
  const prev = readSecretFile<Tokens>(TOKEN_FILE);
  writeSecretFile(TOKEN_FILE, { ...tokens, refresh_token: tokens.refresh_token ?? prev?.refresh_token });
}

async function accessToken(db: DB, force = false): Promise<string> {
  const t = readSecretFile<Tokens>(TOKEN_FILE);
  if (!t?.refresh_token) throw new ReconnectError('Google is not connected.');
  if (!force && t.expires_at - 60_000 > Date.now()) return t.access_token;
  const fresh = await tokenRequest(db, { refresh_token: t.refresh_token, grant_type: 'refresh_token' });
  writeSecretFile(TOKEN_FILE, { ...t, ...fresh, refresh_token: fresh.refresh_token ?? t.refresh_token });
  return fresh.access_token;
}

async function gget(db: DB, url: string): Promise<any> {
  let token = await accessToken(db);
  for (let attempt = 0; attempt < 2; attempt++) {
    let res: Response;
    try {
      res = await fetch(url, { headers: { Authorization: `Bearer ${token}` }, signal: AbortSignal.timeout(30_000) });
    } catch (e) {
      throw new TransientError(`Network error talking to Google: ${(e as Error).message}`);
    }
    if (res.status === 401 && attempt === 0) {
      token = await accessToken(db, true);
      continue;
    }
    const json = (await res.json().catch(() => ({}))) as any;
    if (res.ok) return json;
    const msg = json?.error?.message ?? `HTTP ${res.status}`;
    if (res.status === 429 || res.status >= 500) throw new TransientError(`Google API: ${msg}`);
    if (res.status === 401) throw new ReconnectError('Google rejected the stored sign-in. Reconnect Google.');
    throw new Error(`Google API: ${msg}`);
  }
  throw new ReconnectError('Google rejected the stored sign-in. Reconnect Google.');
}

async function listAll(db: DB, url: string, key: string): Promise<any[]> {
  const out: any[] = [];
  let pageToken: string | undefined;
  do {
    const u = new URL(url);
    if (pageToken) u.searchParams.set('pageToken', pageToken);
    const json = await gget(db, u.toString());
    out.push(...(json[key] ?? []));
    pageToken = json.nextPageToken;
  } while (pageToken);
  return out;
}

/** Classroom due dates are UTC. Without a time we keep the date as given. */
function classroomDue(dueDate: any, dueTime: any, tz: string): string | undefined {
  if (!dueDate?.year) return undefined;
  if (!dueTime || dueTime.hours == null) return `${dueDate.year}-${String(dueDate.month).padStart(2, '0')}-${String(dueDate.day).padStart(2, '0')}`;
  return localParts(tz, new Date(Date.UTC(dueDate.year, dueDate.month - 1, dueDate.day, dueTime.hours ?? 0, dueTime.minutes ?? 0))).date;
}

const CLASSROOM = 'https://classroom.googleapis.com/v1';

export const syncClassroom: Connector = async ({ db, source, tz, today }) => {
  const courses = await listAll(db, `${CLASSROOM}/courses?studentId=me&courseStates=ACTIVE&pageSize=100`, 'courses');
  const items: SourceItem[] = [];
  const calendarIds: string[] = [];
  for (const c of courses) {
    if (c.calendarId) calendarIds.push(c.calendarId);
    const [work, mats, subs] = await Promise.all([
      listAll(db, `${CLASSROOM}/courses/${c.id}/courseWork?pageSize=100`, 'courseWork'),
      listAll(db, `${CLASSROOM}/courses/${c.id}/courseWorkMaterials?pageSize=100`, 'courseWorkMaterial').catch(() => []),
      listAll(db, `${CLASSROOM}/courses/${c.id}/courseWork/-/studentSubmissions?userId=me&pageSize=100`, 'studentSubmissions').catch(() => []),
    ]);
    const doneIds = new Set(subs.filter((s: any) => s.state === 'TURNED_IN' || s.state === 'RETURNED').map((s: any) => s.courseWorkId));
    const examsByTopic = new Map<string, { id: string; due: string }>();
    for (const w of work) {
      const due = classroomDue(w.dueDate, w.dueTime, tz);
      const cls = classify({ title: w.title ?? '', description: w.description, context: 'classroom-work', classroomWorkType: w.workType, hasDue: !!due });
      const kind = cls.kind === 'other' ? 'assignment' : cls.kind;
      items.push({
        externalId: `cw:${w.id}`,
        kind,
        confidence: cls.confidence,
        title: w.title ?? 'Untitled',
        courseName: c.name,
        dueAt: due,
        done: doneIds.has(w.id),
        unitType: kind === 'material' ? 'topics' : undefined,
        quantity: kind === 'material' ? 1 : undefined,
        link: w.alternateLink,
      });
      if ((kind === 'exam' || kind === 'quiz') && w.topicId && due && due > today) {
        const prev = examsByTopic.get(w.topicId);
        if (!prev || due < prev.due) examsByTopic.set(w.topicId, { id: `cw:${w.id}`, due });
      }
    }
    for (const m of mats) {
      items.push({
        externalId: `cwm:${m.id}`,
        kind: 'material',
        confidence: 0.9,
        title: m.title ?? 'Material',
        courseName: c.name,
        unitType: 'topics',
        quantity: Math.max(1, Math.min(5, m.materials?.length ?? 1)),
        deadlineExternalId: m.topicId ? examsByTopic.get(m.topicId)?.id : undefined,
        link: m.alternateLink,
      });
    }
  }
  source.config.courseCalendarIds = calendarIds;
  save();
  return { items, complete: true };
};

/** Strips study keywords to guess the subject: "Chemistry exam" → "Chemistry". */
export function guessCourse(title: string): string {
  const cleaned = title
    .replace(/\b(final|midterm|mid-term|exam|examination|test|quiz|homework|assignment|essay|project|due|egzamin|sprawdzian|kolokwium|kartkówka)\b/giu, '')
    .replace(/[:\-–()]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  return cleaned.split(' ').slice(0, 3).join(' ') || 'General';
}

export async function listCalendars(db: DB): Promise<{ id: string; summary: string; primary: boolean }[]> {
  const cals = await listAll(db, 'https://www.googleapis.com/calendar/v3/users/me/calendarList?maxResults=250', 'items');
  const skip = new Set(db.sources.flatMap((s) => s.config.courseCalendarIds ?? []));
  return cals.filter((c: any) => !skip.has(c.id)).map((c: any) => ({ id: c.id, summary: c.summaryOverride ?? c.summary, primary: !!c.primary }));
}

export const syncCalendar: Connector = async ({ db, source, tz, today }) => {
  const skip = new Set(db.sources.flatMap((s) => s.config.courseCalendarIds ?? []));
  const ids = (source.config.calendarIds?.length ? source.config.calendarIds : ['primary']).filter((id) => !skip.has(id));
  const start = today;
  const end = addDays(today, 120);
  const items: SourceItem[] = [];
  const busy: Omit<BusyEvent, 'sourceId'>[] = [];
  for (const calId of ids) {
    const p = new URLSearchParams({
      timeMin: new Date(`${addDays(start, -1)}T00:00:00Z`).toISOString(),
      timeMax: new Date(`${addDays(end, 1)}T00:00:00Z`).toISOString(),
      singleEvents: 'true',
      orderBy: 'startTime',
      maxResults: '2500',
    });
    const events = await listAll(db, `https://www.googleapis.com/calendar/v3/calendars/${encodeURIComponent(calId)}/events?${p}`, 'items');
    for (const ev of events) {
      if (ev.status === 'cancelled') continue;
      const allDay = !!ev.start?.date;
      const s = allDay ? null : new Date(ev.start.dateTime);
      const e = allDay ? null : new Date(ev.end.dateTime);
      const date = allDay ? ev.start.date : localParts(tz, s!).date;
      if (date < start || date > end) continue;
      const durationMin = s && e ? Math.round((e.getTime() - s.getTime()) / 60000) : undefined;
      const title = ev.summary ?? '';
      const cls = classify({ title, description: ev.description, context: 'calendar', hasDue: true, allDay, durationMin });
      if (cls.kind === 'exam' || cls.kind === 'quiz' || cls.kind === 'assignment') {
        items.push({ externalId: `${calId}:${ev.id}`, kind: cls.kind, confidence: cls.confidence, title, courseName: guessCourse(title), dueAt: date, link: ev.htmlLink });
      } else if (!allDay && ev.transparency !== 'transparent') {
        const sl = localParts(tz, s!);
        const el = localParts(tz, e!);
        busy.push({ date, startMin: sl.minutes, endMin: el.date === date ? el.minutes : 24 * 60, title });
      }
    }
  }
  return { items, busy, complete: true, range: { start, end } };
};
