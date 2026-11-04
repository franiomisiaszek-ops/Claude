import type { ISODate } from './types';

const DAY = 86_400_000;

const toUTC = (d: ISODate) => Date.UTC(+d.slice(0, 4), +d.slice(5, 7) - 1, +d.slice(8, 10));

export const addDays = (d: ISODate, n: number): ISODate => new Date(toUTC(d) + n * DAY).toISOString().slice(0, 10);

/** Whole days from `a` to `b` (positive when `b` is later). */
export const diffDays = (a: ISODate, b: ISODate): number => Math.round((toUTC(b) - toUTC(a)) / DAY);

/** 0 = Sunday. */
export const weekday = (d: ISODate): number => new Date(toUTC(d)).getUTCDay();

export const hhmmToMin = (s: string): number => {
  const [h, m] = s.split(':').map(Number);
  return (h || 0) * 60 + (m || 0);
};

export const minToHhmm = (n: number): string =>
  `${String(Math.floor(n / 60)).padStart(2, '0')}:${String(n % 60).padStart(2, '0')}`;

/** Local calendar date and minutes past midnight for an instant in a timezone. */
export function localParts(tz: string, at: Date = new Date()): { date: ISODate; minutes: number } {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: tz,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(at);
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? '00';
  return { date: `${get('year')}-${get('month')}-${get('day')}`, minutes: +get('hour') * 60 + +get('minute') };
}

export const dateRange = (start: ISODate, end: ISODate): ISODate[] => {
  const out: ISODate[] = [];
  for (let d = start; d <= end; d = addDays(d, 1)) out.push(d);
  return out;
};

export const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

export const fmtDate = (d: ISODate, opts: Intl.DateTimeFormatOptions = { weekday: 'short', month: 'short', day: 'numeric' }) =>
  new Date(toUTC(d)).toLocaleDateString(undefined, { ...opts, timeZone: 'UTC' });

export const fmtMin = (m: number): string => (m >= 60 ? `${Math.floor(m / 60)}h${m % 60 ? ` ${m % 60}m` : ''}` : `${m}m`);
