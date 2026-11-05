export class BadRequest extends Error {}

export const need = (cond: unknown, msg: string): void => {
  if (!cond) throw new BadRequest(msg);
};

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

export const date = (v: unknown, name: string): string => {
  need(typeof v === 'string' && DATE_RE.test(v) && !Number.isNaN(Date.parse(`${v}T00:00:00Z`)), `${name} must be a date (YYYY-MM-DD)`);
  return v as string;
};

export const num = (v: unknown, lo: number, hi: number, name: string): number => {
  const n = Number(v);
  need(Number.isFinite(n) && n >= lo && n <= hi, `${name} must be between ${lo} and ${hi}`);
  return n;
};

export const str = (v: unknown, name: string, max = 300): string => {
  need(typeof v === 'string' && v.trim().length > 0, `${name} is required`);
  return (v as string).trim().slice(0, max);
};

export const optStr = (v: unknown, max = 300): string | undefined => (typeof v === 'string' && v.trim() ? v.trim().slice(0, max) : undefined);

export const oneOf = <T extends string>(v: unknown, options: readonly T[], name: string): T => {
  need(options.includes(v as T), `${name} must be one of: ${options.join(', ')}`);
  return v as T;
};
