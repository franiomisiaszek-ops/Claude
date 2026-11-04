/**
 * Syllabus importer: pasted or uploaded syllabus → proposed exams/deadlines in the review inbox.
 * Claude extracts what each item is and how its date is written; code resolves every date
 * (absolute, or "week 6" relative to the term start), checks each quote against the text,
 * and nothing enters the plan until the user accepts it.
 */
import crypto from 'node:crypto';
import { z } from 'zod';
import { addDays, diffDays } from '../../shared/dates';
import { parseScope } from '../../shared/scope';
import type { ISODate } from '../../shared/types';
import type { DB } from '../db';
import { env } from '../env';
import { callStructured, SAFETY_RULES, untrusted } from './client';
import { redact } from './extract';

const PROMPT_VERSION = 'syllabus-v1';
const WEEKDAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'] as const;

const SyllabusSchema = z.object({
  term_start: z.string(),
  items: z.array(
    z.object({
      kind: z.enum(['exam', 'quiz', 'assignment']),
      title: z.string(),
      date_type: z.enum(['absolute', 'week', 'tbd']),
      year: z.number().int(),
      month: z.number().int(),
      day: z.number().int(),
      week: z.number().int(),
      weekday: z.enum(['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun', 'none']),
      topics: z.string(),
      quote: z.string(),
      confidence: z.enum(['high', 'medium', 'low']),
    }),
  ),
});

const SYSTEM = `You read a course syllabus and list every graded exam, quiz, test and assignment with its date, for a study planner.
For each item give: kind; a short title as written; how the date is written:
- "absolute" with year/month/day (use 0 for an unknown year),
- "week" with the week number and weekday (e.g. "Week 6 Friday"; weekday "none" if not given),
- "tbd" when no date is given.
Also give the topics or chapters it covers as written (e.g. "Chapters 4-6"), and a short quote copied word for word from the syllabus line that states the item.
If the syllabus states when the term or first week starts, return it as term_start in YYYY-MM-DD, otherwise an empty string.
Only list items that are actually in the syllabus. Never guess a date: use "tbd" instead.
${SAFETY_RULES}`;

export interface SyllabusProposal {
  kind: 'exam' | 'quiz' | 'assignment';
  title: string;
  dueAt?: ISODate;
  dateText: string;
  topics: string;
  confidence: number;
  quoteFound: boolean;
}

const norm = (s: string) => s.toLowerCase().normalize('NFKD').replace(/[^\p{L}\p{N}]+/gu, ' ').trim();

/** Picks the year that puts month/day closest to the term (or today), when the syllabus omits it. */
function resolveYear(month: number, day: number, anchor: ISODate): number {
  const y = Number(anchor.slice(0, 4));
  const candidates = [y - 1, y, y + 1].map((yy) => `${yy}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`);
  return Number(candidates.sort((a, b) => Math.abs(diffDays(anchor, a) - 60) - Math.abs(diffDays(anchor, b) - 60))[0].slice(0, 4));
}

const validDate = (y: number, m: number, d: number) => {
  if (m < 1 || m > 12 || d < 1 || d > 31) return null;
  const iso = `${y}-${String(m).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
  const t = Date.parse(`${iso}T00:00:00Z`);
  return Number.isNaN(t) || new Date(t).toISOString().slice(0, 10) !== iso ? null : iso;
};

/** Turns model output into dated proposals. Exported for tests. */
export function resolveItems(raw: z.infer<typeof SyllabusSchema>, text: string, today: ISODate, termStartOverride?: ISODate): SyllabusProposal[] {
  const termStart = termStartOverride || (/^\d{4}-\d{2}-\d{2}$/.test(raw.term_start) ? raw.term_start : undefined);
  const haystack = norm(text);
  return raw.items.slice(0, 200).map((it) => {
    let dueAt: ISODate | undefined;
    let dateText = 'Date not given';
    if (it.date_type === 'absolute' && it.month && it.day) {
      const year = it.year > 1900 ? it.year : resolveYear(it.month, it.day, termStart ?? today);
      dueAt = validDate(year, it.month, it.day) ?? undefined;
      dateText = dueAt ?? 'Invalid date in syllabus';
    } else if (it.date_type === 'week' && it.week > 0) {
      dateText = `Week ${it.week}${it.weekday !== 'none' ? ` ${it.weekday}` : ''}`;
      if (termStart) {
        const monday = addDays(termStart, -((new Date(`${termStart}T00:00:00Z`).getUTCDay() + 6) % 7));
        const offset = it.weekday === 'none' ? 4 : WEEKDAYS.indexOf(it.weekday);
        dueAt = addDays(monday, (it.week - 1) * 7 + offset);
      }
    }
    const quote = norm(it.quote).split(' ').slice(0, 8).join(' ');
    const quoteFound = !!quote && haystack.includes(quote);
    const base = it.confidence === 'high' ? 0.65 : it.confidence === 'medium' ? 0.55 : 0.4;
    return {
      kind: it.kind,
      title: it.title.trim().slice(0, 200) || 'Untitled',
      dueAt,
      dateText,
      topics: it.topics.slice(0, 300),
      // Always below the auto-accept threshold: every syllabus item is reviewed by the user.
      confidence: quoteFound ? base : 0.3,
      quoteFound,
    };
  });
}

export async function analyzeSyllabus(db: DB, userId: string, text: string, today: ISODate, termStart?: ISODate): Promise<SyllabusProposal[]> {
  const clean = redact(text).slice(0, 120_000);
  const hash = crypto.createHash('sha256').update(clean).digest('hex');
  const raw = await callStructured({
    db,
    userId,
    kind: 'syllabus',
    cacheKey: `${PROMPT_VERSION}:${env.anthropic.model}:${hash}`,
    schema: SyllabusSchema,
    system: SYSTEM,
    effort: 'medium',
    content: [{ type: 'text', text: `Today is ${today}.\n\n${untrusted('syllabus', clean)}` }],
  });
  return resolveItems(raw, clean, today, termStart);
}

/** Material suggestions from an item's topic text ("Chapters 4-6" → Chapter 4, 5, 6). */
export function topicsToMaterials(topics: string): { title: string; unitType: 'topics' | 'pages'; quantity: number }[] {
  const scope = parseScope(topics);
  const out: { title: string; unitType: 'topics' | 'pages'; quantity: number }[] = [];
  for (const n of scope.chapters) out.push({ title: `Chapter ${n}`, unitType: 'topics', quantity: 1 });
  for (const n of scope.units) out.push({ title: `Unit ${n}`, unitType: 'topics', quantity: 1 });
  for (const n of scope.lectures) out.push({ title: `Lecture ${n}`, unitType: 'topics', quantity: 1 });
  if (scope.pages && !out.length) out.push({ title: `Pages ${scope.pages[0]}–${scope.pages[1]}`, unitType: 'pages', quantity: scope.pages[1] - scope.pages[0] + 1 });
  return out.slice(0, 40);
}
