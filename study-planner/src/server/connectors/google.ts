/**
 * Google Classroom + Calendar, read-only, through the app's single OAuth client.
 * Each user's refresh token is stored AES-GCM encrypted in `google_connection`.
 */
import crypto from 'node:crypto';
import { eq } from 'drizzle-orm';
import { addDays, localParts } from '../../shared/dates';
import type { BusyEvent, SourceItem } from '../../shared/types';
import { classify } from '../classify';
import { decrypt, encrypt, seal, unseal } from '../crypto';
import type { DB } from '../db';
import { googleConnection } from '../db/schema';
import { env } from '../env';
import { ReconnectError, TransientError, type Connector, type GoogleApi } from './types';

export const GOOGLE_SCOPES = [
  'openid',
  'email',
  'https://www.googleapis.com/auth/classroom.courses.readonly',
  'https://www.googleapis.com/auth/classroom.coursework.me.readonly',
  'https://www.googleapis.com/auth/classroom.courseworkmaterials.readonly',
  'https://www.googleapis.com/auth/classroom.topics.readonly',
  'https://www.googleapis.com/auth/calendar.calendarlist.readonly',
  'https://www.googleapis.com/auth/calendar.events.readonly',
];

export const googleRedirectUri = () => `${env.publicUrl}/oauth/google/callback`;
const b64url = (b: Buffer) => b.toString('base64url');

/** Consent URL. The state is encrypted and bound to the signed-in user. */
export function connectUrl(userId: string): string {
  if (!env.google.clientId) throw new Error('Google is not configured on this server.');
  const verifier = b64url(crypto.randomBytes(32));
  const state = seal({ u: userId, v: verifier }, 15 * 60);
  const p = new URLSearchParams({
    client_id: env.google.clientId,
    redirect_uri: googleRedirectUri(),
    response_type: 'code',
    scope: GOOGLE_SCOPES.join(' '),
    access_type: 'offline',
    prompt: 'consent',
    include_granted_scopes: 'true',
    state,
    code_challenge: b64url(crypto.createHash('sha256').update(verifier).digest()),
    code_challenge_method: 'S256',
  });
  return `https://accounts.google.com/o/oauth2/v2/auth?${p}`;
}

interface TokenResponse {
  access_token: string;
  refresh_token?: string;
  expires_in?: number;
  scope?: string;
  id_token?: string;
}

async function tokenRequest(body: Record<string, string>): Promise<TokenResponse> {
  const res = await fetch('https://oauth2.googleapis.com/token', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ client_id: env.google.clientId!, client_secret: env.google.clientSecret!, ...body }),
    signal: AbortSignal.timeout(20_000),
  });
  const json = (await res.json().catch(() => ({}))) as any;
  if (!res.ok) {
    if (json.error === 'invalid_grant') throw new ReconnectError('Google access expired or was revoked. Reconnect Google.');
    if (res.status >= 500) throw new TransientError('Google sign-in service is unavailable right now.');
    throw new Error(`Google token error: ${json.error_description ?? json.error ?? res.status}`);
  }
  return json as TokenResponse;
}

const emailFromIdToken = (idToken?: string): string | null => {
  if (!idToken) return null;
  try {
    return JSON.parse(Buffer.from(idToken.split('.')[1], 'base64url').toString('utf8')).email ?? null;
  } catch {
    return null;
  }
};

/** Exchanges the code and stores encrypted tokens. `sessionUserId` must match the state's user. */
export async function finishConnect(db: DB, sessionUserId: string, code: string, state: string): Promise<{ email: string | null; missingScopes: string[] }> {
  const st = unseal<{ u: string; v: string }>(state);
  if (st.u !== sessionUserId) throw new Error('This Google connection was started from a different account.');
  const t = await tokenRequest({ code, grant_type: 'authorization_code', redirect_uri: googleRedirectUri(), code_verifier: st.v });
  const granted = new Set((t.scope ?? '').split(' '));
  const missingScopes = GOOGLE_SCOPES.filter((s) => s.startsWith('https://') && !granted.has(s));
  const [prev] = await db.select().from(googleConnection).where(eq(googleConnection.userId, sessionUserId));
  const refresh = t.refresh_token ?? (prev ? decrypt(prev.refreshTokenEnc) : undefined);
  if (!refresh) throw new Error('Google did not return offline access. Please try connecting again.');
  const email = emailFromIdToken(t.id_token);
  const row = {
    userId: sessionUserId,
    googleEmail: email,
    refreshTokenEnc: encrypt(refresh),
    accessTokenEnc: encrypt(t.access_token),
    expiresAt: new Date(Date.now() + (t.expires_in ?? 3600) * 1000),
    scope: t.scope ?? null,
    updatedAt: new Date(),
  };
  await db.insert(googleConnection).values(row).onConflictDoUpdate({ target: googleConnection.userId, set: row });
  return { email, missingScopes };
}

export async function disconnectGoogle(db: DB, userId: string): Promise<void> {
  const [row] = await db.select().from(googleConnection).where(eq(googleConnection.userId, userId));
  if (!row) return;
  try {
    await fetch(`https://oauth2.googleapis.com/revoke?token=${encodeURIComponent(decrypt(row.refreshTokenEnc))}`, {
      method: 'POST',
      signal: AbortSignal.timeout(10_000),
    });
  } catch {
    // Revocation is best effort; the stored token is deleted either way.
  }
  await db.delete(googleConnection).where(eq(googleConnection.userId, userId));
}

export async function googleStatus(db: DB, userId: string): Promise<{ connected: boolean; email?: string | null }> {
  const [row] = await db.select({ email: googleConnection.googleEmail }).from(googleConnection).where(eq(googleConnection.userId, userId));
  return row ? { connected: true, email: row.email } : { connected: false };
}

/** An authorized client that refreshes the access token when needed. */
export async function googleApi(db: DB, userId: string): Promise<GoogleApi> {
  const [row] = await db.select().from(googleConnection).where(eq(googleConnection.userId, userId));
  if (!row) throw new ReconnectError('Google is not connected.');
  let access = row.accessTokenEnc ? decrypt(row.accessTokenEnc) : '';
  let expiresAt = row.expiresAt?.getTime() ?? 0;
  const refresh = async () => {
    const t = await tokenRequest({ refresh_token: decrypt(row.refreshTokenEnc), grant_type: 'refresh_token' });
    access = t.access_token;
    expiresAt = Date.now() + (t.expires_in ?? 3600) * 1000;
    await db
      .update(googleConnection)
      .set({
        accessTokenEnc: encrypt(access),
        expiresAt: new Date(expiresAt),
        ...(t.refresh_token ? { refreshTokenEnc: encrypt(t.refresh_token) } : {}),
        updatedAt: new Date(),
      })
      .where(eq(googleConnection.userId, userId));
  };
  return {
    async get(url: string) {
      if (!access || expiresAt - 60_000 < Date.now()) await refresh();
      for (let attempt = 0; attempt < 2; attempt++) {
        let res: Response;
        try {
          res = await fetch(url, { headers: { Authorization: `Bearer ${access}` }, signal: AbortSignal.timeout(30_000) });
        } catch (e) {
          throw new TransientError(`Network error talking to Google: ${(e as Error).message}`);
        }
        if (res.status === 401 && attempt === 0) {
          await refresh();
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
    },
  };
}

async function listAll(google: GoogleApi, url: string, key: string, maxPages = 20): Promise<any[]> {
  const out: any[] = [];
  let pageToken: string | undefined;
  let pages = 0;
  do {
    const u = new URL(url);
    if (pageToken) u.searchParams.set('pageToken', pageToken);
    const json = await google.get(u.toString());
    out.push(...(json[key] ?? []));
    pageToken = json.nextPageToken;
  } while (pageToken && ++pages < maxPages);
  return out;
}

/** Classroom due dates are UTC. Without a time we keep the date as given. */
function classroomDue(dueDate: any, dueTime: any, tz: string): string | undefined {
  if (!dueDate?.year) return undefined;
  if (!dueTime || dueTime.hours == null) return `${dueDate.year}-${String(dueDate.month).padStart(2, '0')}-${String(dueDate.day).padStart(2, '0')}`;
  return localParts(tz, new Date(Date.UTC(dueDate.year, dueDate.month - 1, dueDate.day, dueTime.hours ?? 0, dueTime.minutes ?? 0))).date;
}

const CLASSROOM = 'https://classroom.googleapis.com/v1';

export const syncClassroom: Connector = async ({ google, tz, today }) => {
  if (!google) throw new ReconnectError('Google is not connected.');
  const courses = await listAll(google, `${CLASSROOM}/courses?studentId=me&courseStates=ACTIVE&pageSize=100`, 'courses');
  const items: SourceItem[] = [];
  const calendarIds: string[] = [];
  for (const c of courses) {
    if (c.calendarId) calendarIds.push(c.calendarId);
    const [work, mats, subs] = await Promise.all([
      listAll(google, `${CLASSROOM}/courses/${c.id}/courseWork?pageSize=100`, 'courseWork'),
      listAll(google, `${CLASSROOM}/courses/${c.id}/courseWorkMaterials?pageSize=100`, 'courseWorkMaterial').catch(() => []),
      listAll(google, `${CLASSROOM}/courses/${c.id}/courseWork/-/studentSubmissions?userId=me&pageSize=100`, 'studentSubmissions').catch(() => []),
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
        description: typeof w.description === 'string' ? w.description.slice(0, 4000) : undefined,
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
        description: typeof m.description === 'string' ? m.description.slice(0, 4000) : undefined,
      });
    }
  }
  return { items, complete: true, configPatch: { courseCalendarIds: calendarIds } };
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

export async function listCalendars(google: GoogleApi, skipIds: string[]): Promise<{ id: string; summary: string; primary: boolean }[]> {
  const cals = await listAll(google, 'https://www.googleapis.com/calendar/v3/users/me/calendarList?maxResults=250', 'items');
  const skip = new Set(skipIds);
  return cals.filter((c: any) => !skip.has(c.id)).map((c: any) => ({ id: c.id, summary: c.summaryOverride ?? c.summary, primary: !!c.primary }));
}

export const syncCalendar: Connector = async ({ google, source, state, tz, today }) => {
  if (!google) throw new ReconnectError('Google is not connected.');
  const skip = new Set(state.sources.flatMap((s) => s.config.courseCalendarIds ?? []));
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
    const events = await listAll(google, `https://www.googleapis.com/calendar/v3/calendars/${encodeURIComponent(calId)}/events?${p}`, 'items', 5);
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
