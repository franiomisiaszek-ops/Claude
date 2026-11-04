/**
 * Material analyzer: turns an uploaded document into study units with exact page/slide ranges.
 * Counting is local and exact; Claude only proposes where units start and end and how dense they are.
 * Code then validates every range and checks each quote against the page it cites.
 */
import crypto from 'node:crypto';
import { z } from 'zod';
import type { Difficulty, UnitType } from '../../shared/types';
import type { DB } from '../db';
import { env, features } from '../env';
import { AiError, assertAiAvailable, callStructured, SAFETY_RULES, untrusted } from './client';
import { extractDocument, pageDigest, type ExtractedDoc } from './extract';

const PROMPT_VERSION = 'material-v1';

export interface MaterialProposal {
  title: string;
  unitType: UnitType;
  quantity: number;
  startPage?: number;
  endPage?: number;
  difficulty: Difficulty;
  confidence: 'high' | 'medium' | 'low';
  evidence?: string;
}

export interface MaterialAnalysis {
  fileName: string;
  doc: { kind: ExtractedDoc['kind']; units: number; unitType: 'pages' | 'slides'; exactCount: boolean; scanned: boolean; words: number };
  method: 'ai' | 'outline' | 'whole';
  items: MaterialProposal[];
  /** Why AI wasn't used, if it wasn't. */
  note?: string;
}

const UnitsSchema = z.object({
  units: z.array(
    z.object({
      title: z.string(),
      start: z.number().int(),
      end: z.number().int(),
      difficulty: z.enum(['easy', 'normal', 'hard']),
      evidence: z.string(),
      confidence: z.enum(['high', 'medium', 'low']),
    }),
  ),
});

const SYSTEM = `You help a study planner split course material into study units (chapters, lectures, sections or problem sets).
You receive a page-by-page digest of one document: each line starts with [page N] or [slide N], then any headings found on it, then the first words of that page.
Return units that follow the document's own structure. Rules:
- Each unit has a clear title taken from the document (e.g. "Chapter 4: Cell structure"), a first and last page or slide number, a difficulty for a typical student (easy: introductory or narrative; normal; hard: dense, mathematical or technical), and a short evidence quote copied word for word from the first words of its first page.
- Units must not overlap and must stay inside the document's page range.
- Leave out front matter and back matter (cover, table of contents, index, bibliography, answer keys) unless they are the main content.
- Prefer 3 to 25 units. Split very long chapters at section headings; merge tiny sections into their chapter.
- Do not estimate study time; the planner computes it from the page counts.
${SAFETY_RULES}`;

const norm = (s: string) => s.toLowerCase().normalize('NFKD').replace(/[^\p{L}\p{N}]+/gu, ' ').trim();

/** Turns raw model ranges into safe, non-overlapping units with exact quantities. */
export function validateUnits(raw: z.infer<typeof UnitsSchema>['units'], doc: ExtractedDoc): MaterialProposal[] {
  const max = doc.units;
  const sorted = raw
    .map((u) => ({ ...u, start: Math.max(1, Math.min(max, Math.round(u.start))), end: Math.max(1, Math.min(max, Math.round(u.end))) }))
    .filter((u) => u.start <= u.end && u.title.trim())
    .sort((a, b) => a.start - b.start || a.end - b.end);
  const out: MaterialProposal[] = [];
  let lastEnd = 0;
  for (const u of sorted.slice(0, 60)) {
    const start = Math.max(u.start, lastEnd + 1);
    if (start > u.end) continue;
    lastEnd = u.end;
    const pageText = norm(doc.pages[start - 1]?.text ?? '');
    const quote = norm(u.evidence).split(' ').slice(0, 6).join(' ');
    const quoteOk = !quote || pageText.includes(quote) || norm(doc.pages[u.start - 1]?.text ?? '').includes(quote);
    out.push({
      title: u.title.trim().slice(0, 200),
      unitType: doc.unitType,
      quantity: u.end - start + 1,
      startPage: start,
      endPage: u.end,
      difficulty: u.difficulty === 'easy' ? 1 : u.difficulty === 'hard' ? 3 : 2,
      confidence: quoteOk ? u.confidence : 'low',
      evidence: u.evidence.slice(0, 200),
    });
  }
  return out;
}

/** Without AI: split at top-level bookmarks/headings, or keep the document as one unit. */
export function localUnits(doc: ExtractedDoc, fileName: string): { method: 'outline' | 'whole'; items: MaterialProposal[] } {
  const top = doc.outline.filter((o) => o.depth === 0);
  const heads = top.length >= 2 ? top : doc.outline.filter((o) => o.depth <= 1);
  if (heads.length >= 2 && heads.length <= 60) {
    const items: MaterialProposal[] = [];
    heads.forEach((h, i) => {
      const end = i + 1 < heads.length ? heads[i + 1].page - 1 : doc.units;
      if (end < h.page) return;
      items.push({ title: h.title, unitType: doc.unitType, quantity: end - h.page + 1, startPage: h.page, endPage: end, difficulty: 2, confidence: 'medium' });
    });
    if (items.length >= 2) return { method: 'outline', items };
  }
  const title = fileName.replace(/\.[a-z0-9]+$/i, '').replace(/[_-]+/g, ' ').trim() || 'Document';
  return { method: 'whole', items: [{ title, unitType: doc.unitType, quantity: doc.units, startPage: 1, endPage: doc.units, difficulty: 2, confidence: 'medium' }] };
}

export async function analyzeMaterial(db: DB, userId: string, file: Buffer, fileName: string, courseName: string): Promise<MaterialAnalysis> {
  const doc = await extractDocument(new Uint8Array(file));
  const summary = { kind: doc.kind, units: doc.units, unitType: doc.unitType, exactCount: doc.exactCount, scanned: doc.scanned, words: doc.words };
  const local = localUnits(doc, fileName);
  if (doc.scanned) {
    return { fileName, doc: summary, ...local, note: `This looks like a scanned document, so its text can't be read. The ${doc.units} pages are counted exactly; split it into chapters yourself if you know them.` };
  }
  if (!features.ai()) return { fileName, doc: summary, ...local, note: 'AI analysis is not set up on this server, so units come from the document outline.' };
  try {
    await assertAiAvailable(db, userId);
  } catch (e) {
    return { fileName, doc: summary, ...local, note: (e as Error).message };
  }
  const hash = crypto.createHash('sha256').update(file).digest('hex');
  try {
    const result = await callStructured({
      db,
      userId,
      kind: 'material',
      cacheKey: `${PROMPT_VERSION}:${env.anthropic.model}:${hash}`,
      schema: UnitsSchema,
      system: SYSTEM,
      effort: 'low',
      content: [
        {
          type: 'text',
          text: `Course: ${courseName.slice(0, 100)}\nDocument: ${doc.kind.toUpperCase()}, ${doc.units} ${doc.unitType}${doc.exactCount ? '' : ' (estimated)'}.\n\n${untrusted('document', pageDigest(doc))}`,
        },
      ],
    });
    const items = validateUnits(result.units, doc);
    if (!items.length) return { fileName, doc: summary, ...local, note: 'The AI found no clear units, so the document outline was used.' };
    return { fileName, doc: summary, method: 'ai', items };
  } catch (e) {
    if (e instanceof AiError) return { fileName, doc: summary, ...local, note: e.message };
    throw e;
  }
}
