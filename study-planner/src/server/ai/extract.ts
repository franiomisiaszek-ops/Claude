/**
 * Local document extraction. Exact counts (pages, slides, words) always come from here,
 * never from the AI. Supports PDF, PPTX, DOCX and plain text, detected by file signature.
 */
import { unzipSync, strFromU8 } from 'fflate';
import { extractText, getDocumentProxy } from 'unpdf';

export type DocKind = 'pdf' | 'pptx' | 'docx' | 'text';

export interface PageText {
  /** 1-based page or slide number. */
  n: number;
  text: string;
}

export interface OutlineEntry {
  title: string;
  page: number;
  depth: number;
}

export interface ExtractedDoc {
  kind: DocKind;
  /** Pages (PDF), slides (PPTX), or estimated pages (DOCX/text, ~400 words each). */
  units: number;
  unitType: 'pages' | 'slides';
  exactCount: boolean;
  pages: PageText[];
  outline: OutlineEntry[];
  /** Little or no text layer: probably a scan. */
  scanned: boolean;
  words: number;
  title?: string;
}

export class ExtractError extends Error {}

const MAX_ZIP_ENTRY = 30_000_000;
const WORDS_PER_PAGE = 400;

export function detectKind(buf: Uint8Array): DocKind | null {
  if (buf.length >= 5 && strFromU8(buf.subarray(0, 5)) === '%PDF-') return 'pdf';
  if (buf.length >= 4 && buf[0] === 0x50 && buf[1] === 0x4b && buf[2] === 0x03 && buf[3] === 0x04) {
    try {
      const ct = unzipSync(buf, { filter: (f) => f.name === '[Content_Types].xml' && f.originalSize < 1_000_000 })['[Content_Types].xml'];
      const xml = ct ? strFromU8(ct) : '';
      if (xml.includes('presentationml')) return 'pptx';
      if (xml.includes('wordprocessingml')) return 'docx';
    } catch {
      return null;
    }
    return null;
  }
  // Plain text: valid UTF-8 without NUL bytes.
  const sample = buf.subarray(0, 4096);
  if (sample.includes(0)) return null;
  try {
    new TextDecoder('utf-8', { fatal: true }).decode(sample.length < buf.length ? buf.subarray(0, Math.min(buf.length, 1_000_000)) : buf);
    return 'text';
  } catch {
    return null;
  }
}

const countWords = (s: string) => (s.match(/[\p{L}\p{N}]+/gu) ?? []).length;
const decodeXml = (s: string) =>
  s
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&#(\d+);/g, (_, d) => String.fromCodePoint(Number(d)))
    .replace(/&amp;/g, '&');

async function extractPdf(buf: Uint8Array): Promise<ExtractedDoc> {
  let pdf;
  try {
    pdf = await getDocumentProxy(new Uint8Array(buf), { isEvalSupported: false, disableFontFace: true } as any);
  } catch (e) {
    throw new ExtractError(/password/i.test(String(e)) ? 'This PDF is password-protected.' : 'Could not read this PDF.');
  }
  const { totalPages, text } = await extractText(pdf, { mergePages: false });
  const pages = text.map((t, i) => ({ n: i + 1, text: t.replace(/\s+/g, ' ').trim() }));
  const outline: OutlineEntry[] = [];
  try {
    const items = (await pdf.getOutline()) ?? [];
    const walk = async (list: any[], depth: number) => {
      for (const it of list) {
        if (outline.length >= 300) return;
        let page = 0;
        try {
          const dest = typeof it.dest === 'string' ? await pdf.getDestination(it.dest) : it.dest;
          if (Array.isArray(dest) && dest[0]) page = (await pdf.getPageIndex(dest[0])) + 1;
        } catch {
          // Unresolvable bookmark: skip its page.
        }
        if (page > 0 && typeof it.title === 'string' && it.title.trim()) outline.push({ title: it.title.trim().slice(0, 200), page, depth });
        if (it.items?.length && depth < 2) await walk(it.items, depth + 1);
      }
    };
    await walk(items, 0);
  } catch {
    // No outline.
  }
  const chars = pages.reduce((a, p) => a + p.text.length, 0);
  const words = pages.reduce((a, p) => a + countWords(p.text), 0);
  await pdf.destroy();
  return {
    kind: 'pdf',
    units: totalPages,
    unitType: 'pages',
    exactCount: true,
    pages,
    outline: outline.sort((a, b) => a.page - b.page || a.depth - b.depth),
    scanned: totalPages > 0 && chars / totalPages < 50,
    words,
  };
}

function extractPptx(buf: Uint8Array): ExtractedDoc {
  const files = unzipSync(buf, { filter: (f) => /^ppt\/slides\/slide\d+\.xml$/.test(f.name) && f.originalSize < MAX_ZIP_ENTRY });
  const slides = Object.keys(files)
    .map((name) => ({ n: Number(name.match(/slide(\d+)\.xml$/)![1]), xml: strFromU8(files[name]) }))
    .sort((a, b) => a.n - b.n);
  if (!slides.length) throw new ExtractError('No slides found in this presentation.');
  const pages: PageText[] = [];
  const outline: OutlineEntry[] = [];
  slides.forEach((s, i) => {
    const runs = [...s.xml.matchAll(/<a:t>([^<]*)<\/a:t>/g)].map((m) => decodeXml(m[1]));
    const text = runs.join(' ').replace(/\s+/g, ' ').trim();
    pages.push({ n: i + 1, text });
    const titleBlock = s.xml.match(/<p:sp>(?:(?!<\/p:sp>).)*?type="(?:title|ctrTitle)"(?:(?!<\/p:sp>).)*?<\/p:sp>/s)?.[0];
    const title = titleBlock ? [...titleBlock.matchAll(/<a:t>([^<]*)<\/a:t>/g)].map((m) => decodeXml(m[1])).join(' ').trim() : '';
    if (title) outline.push({ title: title.slice(0, 200), page: i + 1, depth: 0 });
  });
  const words = pages.reduce((a, p) => a + countWords(p.text), 0);
  return { kind: 'pptx', units: slides.length, unitType: 'slides', exactCount: true, pages, outline, scanned: false, words };
}

/** Splits running text into ~400-word "pages" so long documents can be described page by page. */
function paginate(paragraphs: { text: string; heading?: number }[]): { pages: PageText[]; outline: OutlineEntry[]; words: number } {
  const pages: PageText[] = [];
  const outline: OutlineEntry[] = [];
  let cur: string[] = [];
  let curWords = 0;
  let words = 0;
  const flush = () => {
    if (!cur.length) return;
    pages.push({ n: pages.length + 1, text: cur.join(' ') });
    cur = [];
    curWords = 0;
  };
  for (const p of paragraphs) {
    const w = countWords(p.text);
    if (!w) continue;
    if (p.heading != null) outline.push({ title: p.text.slice(0, 200), page: pages.length + 1, depth: Math.max(0, p.heading - 1) });
    cur.push(p.text);
    curWords += w;
    words += w;
    if (curWords >= WORDS_PER_PAGE) flush();
  }
  flush();
  return { pages, outline, words };
}

function extractDocx(buf: Uint8Array): ExtractedDoc {
  const files = unzipSync(buf, { filter: (f) => f.name === 'word/document.xml' && f.originalSize < MAX_ZIP_ENTRY });
  const xml = files['word/document.xml'] ? strFromU8(files['word/document.xml']) : '';
  if (!xml) throw new ExtractError('Could not read this Word document.');
  const paragraphs = [...xml.matchAll(/<w:p[ >](?:(?!<\/w:p>).)*<\/w:p>/gs)].map((m) => {
    const p = m[0];
    const text = decodeXml([...p.matchAll(/<w:t[^>]*>([^<]*)<\/w:t>/g)].map((t) => t[1]).join('')).trim();
    const level = p.match(/<w:pStyle w:val="(?:Heading|heading|Nagwek|Titre|berschrift)(\d)"/)?.[1];
    return { text, heading: level ? Number(level) : undefined };
  });
  const { pages, outline, words } = paginate(paragraphs);
  return { kind: 'docx', units: Math.max(1, pages.length), unitType: 'pages', exactCount: false, pages, outline, scanned: false, words };
}

function extractPlain(buf: Uint8Array): ExtractedDoc {
  const text = new TextDecoder('utf-8').decode(buf);
  const paragraphs = text.split(/\r?\n/).map((line) => {
    const t = line.trim();
    const md = t.match(/^(#{1,3})\s+(.*)$/);
    if (md) return { text: md[2], heading: md[1].length };
    const numbered = /^(chapter|unit|section|lecture|part|week|topic)\s+\d+/i.test(t);
    return { text: t, heading: numbered ? 1 : undefined };
  });
  const { pages, outline, words } = paginate(paragraphs);
  return { kind: 'text', units: Math.max(1, pages.length), unitType: 'pages', exactCount: false, pages, outline, scanned: false, words };
}

export async function extractDocument(buf: Uint8Array): Promise<ExtractedDoc> {
  const kind = detectKind(buf);
  if (!kind) throw new ExtractError('Unsupported file. Upload a PDF, PowerPoint (.pptx), Word (.docx) or text file.');
  if (kind === 'pdf') return extractPdf(buf);
  if (kind === 'pptx') return extractPptx(buf);
  if (kind === 'docx') return extractDocx(buf);
  return extractPlain(buf);
}

/** Removes contact details before anything leaves the server. */
export function redact(text: string): string {
  return text
    .replace(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi, '[email]')
    .replace(/(?:\+?\d[\d ()-]{7,}\d)/g, (m) => (m.replace(/\D/g, '').length >= 9 ? '[phone]' : m));
}

/**
 * A compact per-page digest (~150 tokens/page): headings and the start of each page.
 * Much cheaper than sending the document, and enough to find structure.
 */
export function pageDigest(doc: ExtractedDoc, maxPages = 400): string {
  const lines: string[] = [];
  const outlineByPage = new Map<number, string[]>();
  for (const o of doc.outline) outlineByPage.set(o.page, [...(outlineByPage.get(o.page) ?? []), `${'  '.repeat(o.depth)}${o.title}`]);
  const step = doc.pages.length > maxPages ? Math.ceil(doc.pages.length / maxPages) : 1;
  for (let i = 0; i < doc.pages.length; i += step) {
    const p = doc.pages[i];
    const words = redact(p.text).split(/\s+/).filter(Boolean);
    const heads = outlineByPage.get(p.n);
    lines.push(`[${doc.unitType === 'slides' ? 'slide' : 'page'} ${p.n}]${heads ? ` headings: ${heads.join(' | ')}` : ''} ${words.slice(0, 40).join(' ')}${words.length > 40 ? ' …' : ''} (${words.length} words)`);
  }
  return lines.join('\n');
}
