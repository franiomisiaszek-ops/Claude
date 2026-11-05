import type { DeadlineKind } from '../shared/types';

export type Kind = DeadlineKind | 'material' | 'other';

export interface Classification {
  kind: Kind;
  confidence: number;
}

/** Keywords in English plus several common languages (Polish, German, Spanish, French, Italian, Dutch, Portuguese). */
const KEYWORDS: Record<'exam' | 'quiz' | 'assignment', string[]> = {
  exam: [
    'exam', 'midterm', 'mid-term', 'final', 'finals', 'test', 'examination',
    'egzamin', 'kolokwium', 'sprawdzian', 'matura',
    'prüfung', 'klausur', 'examen', 'parcial', 'esame', 'verifica', 'tentamen', 'toets', 'prova', 'partiel', 'contrôle',
  ],
  quiz: ['quiz', 'quizz', 'kartkówka', 'kartkowka', 'cuestionario', 'questionnaire', 'pop quiz'],
  assignment: [
    'homework', 'assignment', 'hw', 'essay', 'project', 'report', 'lab', 'problem set', 'pset', 'worksheet', 'submission', 'presentation',
    'zadanie', 'praca domowa', 'projekt', 'referat', 'wypracowanie', 'prezentacja',
    'hausaufgabe', 'abgabe', 'tarea', 'deber', 'devoir', 'compito', 'huiswerk', 'trabalho',
  ],
};

const norm = (s: string) => s.toLowerCase().normalize('NFC');

function matches(text: string, kw: string): boolean {
  const escaped = kw.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return new RegExp(`(^|[^\\p{L}\\p{N}])${escaped}($|[^\\p{L}\\p{N}])`, 'u').test(text);
}

function keywordKind(title: string, description = ''): { kind: 'exam' | 'quiz' | 'assignment'; inTitle: boolean } | null {
  const t = norm(title);
  const d = norm(description).slice(0, 500);
  for (const kind of ['quiz', 'exam', 'assignment'] as const) {
    if (KEYWORDS[kind].some((k) => matches(t, k))) return { kind, inTitle: true };
  }
  for (const kind of ['quiz', 'exam', 'assignment'] as const) {
    if (KEYWORDS[kind].some((k) => matches(d, k))) return { kind, inTitle: false };
  }
  return null;
}

export interface ClassifyInput {
  title: string;
  description?: string;
  /** Where the item came from, which changes the priors. */
  context: 'classroom-work' | 'classroom-material' | 'lms-feed' | 'calendar';
  classroomWorkType?: string;
  hasDue: boolean;
  allDay?: boolean;
  durationMin?: number;
  /** Local time of day in minutes, if timed. */
  startMin?: number;
}

/**
 * Rule-based classifier. Confidence below 0.7 sends the item to the inbox for review.
 */
export function classify(i: ClassifyInput): Classification {
  const kw = keywordKind(i.title, i.description);
  if (i.context === 'classroom-material') return { kind: 'material', confidence: 0.9 };

  if (i.context === 'classroom-work') {
    if (!i.hasDue) return { kind: 'material', confidence: 0.8 };
    if (kw && kw.kind !== 'assignment') return { kind: kw.kind, confidence: kw.inTitle ? 0.9 : 0.7 };
    return { kind: 'assignment', confidence: 0.85 };
  }

  if (i.context === 'lms-feed') {
    if (kw) return { kind: kw.kind, confidence: kw.inTitle ? 0.9 : 0.7 };
    // LMS deadlines are usually due at 23:59 or all-day.
    if (i.allDay || (i.startMin != null && i.startMin >= 23 * 60)) return { kind: 'assignment', confidence: 0.75 };
    return { kind: 'assignment', confidence: 0.55 };
  }

  // Personal calendar: most events are not study items.
  if (kw && kw.kind !== 'assignment' && kw.inTitle) {
    const examShaped = !i.allDay && (i.durationMin ?? 0) >= 30 && (i.durationMin ?? 0) <= 300;
    return { kind: kw.kind, confidence: examShaped ? 0.85 : 0.65 };
  }
  if (kw && kw.inTitle) return { kind: 'assignment', confidence: 0.6 };
  return { kind: 'other', confidence: 0.9 };
}

/** Canvas-style feeds put the course code in brackets at the end: "HW3 [BIO101]". */
export function splitCourse(title: string): { title: string; course?: string } {
  const m = title.match(/^(.*?)\s*\[([^\]]+)\]\s*$/);
  return m ? { title: m[1].trim(), course: m[2].trim() } : { title: title.trim() };
}

/** Parses a pasted line like "Chapter 3 – 24 pages" or "Lecture 5 slides (40)". */
export function parseMaterialLine(line: string): { title: string; unitType: 'pages' | 'slides' | 'problems' | 'videoMinutes' | 'topics' | 'minutes'; quantity: number } | null {
  const text = line.replace(/^\s*(?:[-*•]+|\d+[.)])\s+/, '').trim();
  if (!text) return null;
  const units: [RegExp, 'pages' | 'slides' | 'problems' | 'videoMinutes' | 'minutes'][] = [
    [/(\d+)\s*(?:pages?|pp?\.?|str\.?|stron[ay]?|seiten)\b/i, 'pages'],
    [/(?:pages?|pp?\.?)\s*(\d+)\s*[-–]\s*(\d+)/i, 'pages'],
    [/(\d+)\s*(?:slides?|slajd(?:y|ów)?|folien)\b/i, 'slides'],
    [/(\d+)\s*(?:problems?|exercises?|questions?|zada[nń]|ćwicze[nń])\b/i, 'problems'],
    [/(\d+)\s*(?:min(?:ute)?s?)\s*(?:video|lecture|recording)/i, 'videoMinutes'],
    [/(\d+)\s*(?:h|hours?|godz(?:in)?)\b/i, 'minutes'],
    [/(\d+)\s*(?:min(?:ute)?s?)\b/i, 'minutes'],
  ];
  for (const [re, unitType] of units) {
    const m = text.match(re);
    if (!m) continue;
    let quantity = +m[1];
    if (m[2]) quantity = Math.max(1, +m[2] - +m[1] + 1);
    if (/h|hour|godz/i.test(m[0]) && unitType === 'minutes' && !/min/i.test(m[0])) quantity *= 60;
    const title = text.replace(m[0], '').replace(/[\s\-–:,()]+$/g, '').replace(/^[\s\-–:,()]+/g, '').trim() || text;
    return { title, unitType, quantity };
  }
  return { title: text, unitType: 'topics', quantity: 1 };
}
