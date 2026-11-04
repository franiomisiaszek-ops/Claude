import ical from 'node-ical';
import { addDays, localParts } from '../../shared/dates';
import type { BusyEvent, SourceItem } from '../../shared/types';
import { classify, splitCourse } from '../classify';
import { guessCourse } from './google';
import { FetchBlockedError, safeFetch } from '../safe-fetch';
import { TransientError, type Connector } from './types';

const ymdLocal = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;

/** Parses an iCalendar document into study items and busy time. Exported for tests. */
export function parseFeed(text: string, opts: { tz: string; today: string; feedType: 'lms' | 'calendar'; fallbackCourse: string }) {
  const parsed = ical.sync.parseICS(text);
  const start = addDays(opts.today, -14);
  const end = addDays(opts.today, 180);
  const items: SourceItem[] = [];
  const busy: Omit<BusyEvent, 'sourceId'>[] = [];
  for (const ev of Object.values(parsed)) {
    if (!ev || ev.type !== 'VEVENT') continue;
    if ((ev as any).status === 'CANCELLED') continue;
    let instances;
    try {
      instances = ical.expandRecurringEvent(ev, { from: new Date(`${start}T00:00:00Z`), to: new Date(`${end}T23:59:59Z`) });
    } catch {
      continue;
    }
    for (const inst of instances) {
      const allDay = inst.isFullDay;
      const s = new Date(inst.start);
      const e = inst.end ? new Date(inst.end) : s;
      const date = allDay ? ymdLocal(s) : localParts(opts.tz, s).date;
      if (date < start || date > end) continue;
      const startMin = allDay ? undefined : localParts(opts.tz, s).minutes;
      const durationMin = Math.round((e.getTime() - s.getTime()) / 60000);
      const rawTitle = typeof inst.summary === 'string' ? inst.summary : String((inst.summary as any)?.val ?? 'Untitled');
      const description = typeof ev.description === 'string' ? ev.description : '';
      const split = splitCourse(rawTitle);
      const cls = classify({
        title: split.title,
        description,
        context: opts.feedType === 'lms' ? 'lms-feed' : 'calendar',
        hasDue: true,
        allDay,
        durationMin,
        startMin,
      });
      if (cls.kind === 'other' || cls.kind === 'material') {
        if (opts.feedType === 'calendar' && !allDay && (ev as any).transparency !== 'TRANSPARENT') {
          const endLocal = localParts(opts.tz, e);
          busy.push({ date, startMin: startMin!, endMin: endLocal.date === date ? endLocal.minutes : 24 * 60, title: rawTitle });
        }
        continue;
      }
      const categories = Array.isArray((ev as any).categories) ? (ev as any).categories : [];
      items.push({
        externalId: inst.isRecurring ? `${ev.uid}@${date}` : String(ev.uid),
        kind: cls.kind,
        confidence: cls.confidence,
        title: split.title,
        courseName: split.course ?? categories[0] ?? (opts.feedType === 'calendar' ? guessCourse(split.title) : opts.fallbackCourse),
        dueAt: date,
        link: typeof (ev as any).url === 'string' ? (ev as any).url : undefined,
      });
    }
  }
  return { items, busy, range: { start, end } };
}

/** Any iCalendar feed: Canvas, Moodle, Blackboard, Brightspace, Outlook, Apple, Google "secret address". */
export const syncIcs: Connector = async ({ source, tz, today }) => {
  const url = (source.config.url ?? '').trim().replace(/^webcal:/i, 'https:');
  if (!/^https?:\/\//i.test(url)) throw new Error('Feed URL must start with https:// or webcal://');
  const headers: Record<string, string> = {};
  if (source.etag) headers['If-None-Match'] = source.etag;
  if (source.lastModified) headers['If-Modified-Since'] = source.lastModified;
  let res;
  try {
    res = await safeFetch(url, { headers, timeoutMs: 30_000, maxBytes: 8_000_000 });
  } catch (e) {
    if (e instanceof FetchBlockedError) throw new Error(e.message);
    throw new TransientError(`Could not reach the feed: ${(e as Error).message}`);
  }
  if (res.status === 304) return { items: [], complete: false, notModified: true };
  if (res.status === 429 || res.status >= 500) throw new TransientError(`Feed returned HTTP ${res.status}`);
  if (res.status < 200 || res.status >= 300) throw new Error(`Feed returned HTTP ${res.status}. Check that the URL is still valid.`);
  if (!res.body.includes('BEGIN:VCALENDAR')) throw new Error('That URL did not return an iCalendar (.ics) feed.');
  const { items, busy, range } = parseFeed(res.body, { tz, today, feedType: source.config.feedType ?? 'lms', fallbackCourse: source.name });
  const header = (k: string) => {
    const v = res.headers[k];
    return Array.isArray(v) ? v[0] : v;
  };
  return { items, busy, complete: true, range, etag: header('etag'), lastModified: header('last-modified') };
};
