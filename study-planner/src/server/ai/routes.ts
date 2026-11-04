import express, { type Request, type Response, type Router } from 'express';
import rateLimit from 'express-rate-limit';
import crypto from 'node:crypto';
import { z } from 'zod';
import { matchScope, parseScope, scopeIsEmpty } from '../../shared/scope';
import type { PublicState, UserState } from '../../shared/types';
import type { Mutation } from '../app';
import { randomId } from '../crypto';
import type { DB } from '../db';
import { env } from '../env';
import { findOrCreateCourse } from '../merge';
import { todayFor } from '../plan-service';
import { loadState } from '../repo';
import { date, need, optStr, str } from '../validate';
import { AiError, callStructured, SAFETY_RULES, untrusted } from './client';
import { extractDocument, ExtractError, redact } from './extract';
import { analyzeMaterial } from './material';
import { analyzeSyllabus, topicsToMaterials } from './syllabus';

interface Deps {
  db: DB;
  applyMutation: (req: Request, label: string | null, fn: Mutation) => Promise<UserState>;
  mutate: (label: string | null, fn: Mutation) => (req: Request, res: Response) => Promise<void>;
  fail: (res: Response, e: unknown) => unknown;
  respondState: (req: Request, res: Response) => Promise<void>;
  publicState: (state: UserState, user: any) => Promise<PublicState>;
  aiAllowedFor: (state: Pick<UserState, 'settings'>) => boolean;
}

/** One document analysis at a time (free instances have 512 MB), with a short queue. */
function createGate(maxWaiting: number) {
  let chain: Promise<unknown> = Promise.resolve();
  let waiting = 0;
  return async <T>(fn: () => Promise<T>): Promise<T> => {
    if (waiting >= maxWaiting) throw new AiError('The server is busy reading other documents. Try again in a minute.');
    waiting++;
    const run = chain.then(fn, fn);
    chain = run.catch(() => {}).finally(() => waiting--);
    return run;
  };
}

const MAX_UPLOAD = 25 * 1024 * 1024;

export function registerAiRoutes(api: Router, d: Deps) {
  const perUser = rateLimit({
    windowMs: 60_000,
    limit: 8,
    keyGenerator: (req) => req.user?.id ?? 'anon',
    standardHeaders: 'draft-8',
    legacyHeaders: false,
    message: { error: 'Too many analyses at once. Wait a minute and try again.' },
  });
  const raw = express.raw({ type: () => true, limit: MAX_UPLOAD });
  const gate = createGate(3);
  /** Under-18 users keep every non-AI path; AI is for adults by default. */
  const adultsOnly = async (req: Request, res: Response, next: () => void) => {
    const st = await loadState(d.db, req.user!.id);
    if (!d.aiAllowedFor(st)) return res.status(403).json({ error: 'AI features are available for adults (18+). You can add material by hand or paste a list.' });
    next();
  };

  const aiFail = (res: Response, e: unknown) => {
    if (e instanceof AiError || e instanceof ExtractError) return res.status(400).json({ error: e.message });
    return d.fail(res, e);
  };

  /** Upload a document → proposed study units (nothing is saved until the user accepts). */
  api.post('/ai/material', perUser, raw, async (req, res) => {
    // Local extraction (exact page counts) works for everyone; Claude is only used for adults.
    try {
      const body = req.body as Buffer;
      need(Buffer.isBuffer(body) && body.length > 0, 'Choose a file to upload');
      const st = await loadState(d.db, req.user!.id);
      const course = st.courses.find((c) => c.id === req.query.courseId);
      const fileName = optStr(req.query.fileName, 200) ?? 'Document';
      res.json(await gate(() => analyzeMaterial(d.db, req.user!.id, body, fileName, course?.name ?? '', { useAi: d.aiAllowedFor(st) })));
    } catch (e) {
      aiFail(res, e);
    }
  });

  /** Syllabus (pasted text, or a file as the raw body) → proposals in the inbox for review. */
  api.post('/ai/syllabus', perUser, adultsOnly, raw, async (req, res) => {
    try {
      const body = req.body as Buffer;
      need(Buffer.isBuffer(body) && body.length > 0, 'Paste the syllabus or upload a file');
      let text: string;
      if (req.get('content-type')?.startsWith('text/plain')) text = body.toString('utf8');
      else {
        const doc = await gate(() => extractDocument(new Uint8Array(body)));
        need(!doc.scanned, 'This looks like a scanned document, so its text can\'t be read. Paste the syllabus text instead.');
        text = doc.pages.map((p) => p.text).join('\n');
      }
      need(text.trim().length >= 40, 'That syllabus is too short to read.');
      const termStart = req.query.termStart ? date(req.query.termStart, 'Term start') : undefined;
      const snapshotState = await loadState(d.db, req.user!.id);
      const dateOrder = ['DMY', 'MDY', 'YMD'].includes(String(req.query.dateOrder)) ? (String(req.query.dateOrder) as 'DMY' | 'MDY' | 'YMD') : undefined;
      const proposals = await analyzeSyllabus(d.db, req.user!.id, text, todayFor(snapshotState), termStart, dateOrder);
      const courseName = str(req.query.courseName ?? 'Syllabus', 'Course', 80);
      let added = 0;
      const state = await d.applyMutation(req, null, (st) => {
        const course = req.query.courseId ? st.courses.find((c) => c.id === req.query.courseId) : findOrCreateCourse(st, courseName);
        need(course, 'Course not found');
        const norm = (s: string) => s.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').trim();
        for (const p of proposals) {
          const dup = st.deadlines.some((x) => x.courseId === course!.id && norm(x.title) === norm(p.title) && (!p.dueAt || Math.abs(Date.parse(x.dueAt) - Date.parse(p.dueAt)) <= 86_400_000));
          if (dup) continue;
          const externalId = `syl:${crypto.createHash('sha1').update(`${course!.id}|${p.title}|${p.dateText}`).digest('hex').slice(0, 16)}`;
          if (st.inbox.some((e) => e.externalId === externalId && !e.resolved)) continue;
          st.inbox.push({
            id: randomId('i_'),
            change: 'new',
            entity: 'deadline',
            sourceId: 'syllabus',
            externalId,
            title: p.title,
            payload: {
              item: { externalId, kind: p.kind, confidence: p.confidence, title: p.title, courseName: course!.name, dueAt: p.dueAt },
              materials: topicsToMaterials(p.topics),
              dateText: p.dateText,
              topics: p.topics,
              quoteFound: p.quoteFound,
              ambiguousDate: p.ambiguousDate,
            },
            createdAt: new Date().toISOString(),
            resolved: false,
          });
          added++;
        }
      });
      res.json({ added, found: proposals.length, state: await d.publicState(state, req.user!) });
    } catch (e) {
      aiFail(res, e);
    }
  });

  /**
   * Exam scope from its Classroom/LMS description ("Test on chapters 4–6").
   * Pattern matching first; Claude only when the text is free-form.
   */
  api.post('/ai/scope/:deadlineId', perUser, adultsOnly, async (req, res) => {
    try {
      const st = await loadState(d.db, req.user!.id);
      const dl = st.deadlines.find((x) => x.id === req.params.deadlineId);
      need(dl, 'Deadline not found');
      const text = optStr(req.body?.text, 4000) ?? String((dl!.origin?.fields as any)?.description ?? '');
      need(text.trim(), 'There is no description to read for this deadline.');
      const courseMaterials = st.materials.filter((m) => m.courseId === dl!.courseId).map((m) => ({ id: m.id, title: m.title }));
      const scope = parseScope(text);
      if (!scopeIsEmpty(scope)) return res.json({ method: 'pattern', ...matchScope(scope, courseMaterials) });
      const Schema = z.object({ matched_ids: z.array(z.string()), new_items: z.array(z.object({ title: z.string() })) });
      const out = await callStructured({
        db: d.db,
        userId: req.user!.id,
        kind: 'scope',
        cacheKey: `scope-v1:${env.anthropic.model}:${crypto.createHash('sha256').update(text + JSON.stringify(courseMaterials)).digest('hex')}`,
        schema: Schema,
        system: `You map an exam description to the course's study materials. Return the ids of materials the description says are on the exam (only ids from the list), and titles of topics it names that are not in the list. Return nothing when the description doesn't say what is covered.\n${SAFETY_RULES}`,
        effort: 'low',
        content: [{ type: 'text', text: `Materials:\n${courseMaterials.map((m) => `${m.id}: ${m.title}`).join('\n') || '(none)'}\n\n${untrusted('description', redact(text))}` }],
      });
      const ids = new Set(courseMaterials.map((m) => m.id));
      res.json({
        method: 'ai',
        matched: out.matched_ids.filter((id) => ids.has(id)),
        missing: out.new_items.slice(0, 30).map((i) => ({ title: i.title.slice(0, 120), kind: 'unit' as const })),
      });
    } catch (e) {
      aiFail(res, e);
    }
  });
}
