/**
 * Reads exam scope from text like "Test on chapters 4–6", "Unit 3 and 4", "pp. 10-40", "Lectures 5-7".
 * Pure and shared: the server uses it on Classroom descriptions, the UI for instant suggestions.
 */
export interface Scope {
  chapters: number[];
  units: number[];
  lectures: number[];
  sections: string[];
  pages?: [number, number];
}

const WORD = {
  chapters: /(?:chapters?|chap\.?|ch\.?|rozdzia(?:ł|ły|łów)|kapitel|chapitres?|cap[ií]tulos?)/i,
  units: /(?:units?|modules?|topics?|dzia(?:ł|ły|łów)|themen?|unidad(?:es)?|unités?)/i,
  lectures: /(?:lectures?|lessons?|wyk(?:ład|łady)|vorlesung(?:en)?|lekcj[ae])/i,
};

function expand(list: string): number[] {
  const out = new Set<number>();
  for (const part of list.split(/\s*(?:,|;|&|\band\b|\boraz\b|\bi\b|\bund\b|\bet\b|\by\b)\s*/i)) {
    const range = part.match(/^(\d{1,3})\s*(?:-|–|—|to|through|do|bis|à|a)\s*(\d{1,3})$/i);
    if (range) {
      const a = Number(range[1]);
      const b = Number(range[2]);
      if (b >= a && b - a <= 40) for (let i = a; i <= b; i++) out.add(i);
      continue;
    }
    const single = part.match(/^(\d{1,3})$/);
    if (single) out.add(Number(single[1]));
  }
  return [...out].sort((a, b) => a - b);
}

const LIST = String.raw`(\d{1,3}(?:\s*(?:-|–|—|to|through|do|bis)\s*\d{1,3})?(?:\s*(?:,|;|&|and|oraz|i|und|et|y)\s*\d{1,3}(?:\s*(?:-|–|—|to|through|do|bis)\s*\d{1,3})?)*)`;

export function parseScope(text: string): Scope {
  const t = ` ${text.replace(/\s+/g, ' ')} `;
  const grab = (word: RegExp) => {
    const re = new RegExp(`${word.source}\\s*${LIST}`, 'gi');
    const nums = new Set<number>();
    for (const m of t.matchAll(re)) for (const n of expand(m[1])) nums.add(n);
    return [...nums].sort((a, b) => a - b);
  };
  const scope: Scope = { chapters: grab(WORD.chapters), units: grab(WORD.units), lectures: grab(WORD.lectures), sections: [] };
  for (const m of t.matchAll(/(?:§|sections?|sec\.)\s*(\d+(?:\.\d+)+(?:\s*(?:-|–)\s*\d+(?:\.\d+)+)?)/gi)) scope.sections.push(m[1].replace(/\s/g, ''));
  const pages = t.match(/(?:pages?|pp?\.|str\.|seiten?|s\.)\s*(\d{1,4})\s*(?:-|–|—|to)\s*(\d{1,4})/i);
  if (pages) {
    const a = Number(pages[1]);
    const b = Number(pages[2]);
    if (b >= a && b - a < 2000) scope.pages = [a, b];
  }
  return scope;
}

export const scopeIsEmpty = (s: Scope) => !s.chapters.length && !s.units.length && !s.lectures.length && !s.sections.length && !s.pages;

/** Number a material title refers to, e.g. "Ch. 4 Cell structure" → chapter 4. */
function numberIn(title: string, word: RegExp): number | null {
  const m = title.match(new RegExp(`${word.source}\\s*(\\d{1,3})\\b`, 'i'));
  return m ? Number(m[1]) : null;
}

export interface ScopeMatch {
  /** Existing materials that the scope names. */
  matched: string[];
  /** Parts of the scope with no material yet, as suggested titles. */
  missing: { title: string; kind: 'chapter' | 'unit' | 'lecture' | 'pages'; quantity?: number }[];
}

export function matchScope(scope: Scope, materials: { id: string; title: string }[]): ScopeMatch {
  const matched = new Set<string>();
  const missing: ScopeMatch['missing'] = [];
  const check = (nums: number[], word: RegExp, kind: 'chapter' | 'unit' | 'lecture', label: string) => {
    for (const n of nums) {
      const hit = materials.find((m) => numberIn(m.title, word) === n);
      if (hit) matched.add(hit.id);
      else missing.push({ title: `${label} ${n}`, kind });
    }
  };
  check(scope.chapters, WORD.chapters, 'chapter', 'Chapter');
  check(scope.units, WORD.units, 'unit', 'Unit');
  check(scope.lectures, WORD.lectures, 'lecture', 'Lecture');
  if (scope.pages && !scope.chapters.length) missing.push({ title: `Pages ${scope.pages[0]}–${scope.pages[1]}`, kind: 'pages', quantity: scope.pages[1] - scope.pages[0] + 1 });
  return { matched: [...matched], missing };
}
