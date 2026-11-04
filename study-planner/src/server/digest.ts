import nodemailer from 'nodemailer';
import { addDays, diffDays, fmtMin, hhmmToMin, localParts } from '../shared/dates';
import type { DB, ISODate } from '../shared/types';
import { sessionTitle } from '../shared/labels';

export { sessionTitle };
import { replan, todayFor } from './plan-service';
import { load, save } from './store';
import { syncAll } from './sync';

export interface Digest {
  date: ISODate;
  dayOff: boolean;
  totalMin: number;
  subject: string;
  lines: string[];
  text: string;
}

/** The daily message: what to study today and for how long, or that it's a day off. */
export function buildDigest(db: DB, date: ISODate = todayFor(db)): Digest {
  const today = db.sessions.filter((s) => s.date === date && s.status === 'planned');
  const totalMin = today.reduce((a, s) => a + s.minutes, 0);
  const lines: string[] = [];
  let subject: string;
  if (!today.length) {
    const next = db.sessions.find((s) => s.date > date && s.status === 'planned');
    const blocked = db.blocks.some((b) => b.start <= date && date <= b.end);
    subject = blocked ? 'Day off today: you blocked this day' : 'Day off today';
    lines.push(next ? `Nothing to study today. Next session: ${next.date} (${sessionTitle(db, next)}).` : 'Nothing to study today and nothing planned yet.');
  } else {
    subject = `Today: ${fmtMin(totalMin)} of study (${today.length} session${today.length > 1 ? 's' : ''})`;
    for (const s of today) lines.push(`• ${sessionTitle(db, s)}: ${fmtMin(s.minutes)}`);
  }
  const soon = db.deadlines
    .filter((d) => d.status === 'active' && d.dueAt >= date && d.dueAt <= addDays(date, 7))
    .sort((a, b) => a.dueAt.localeCompare(b.dueAt));
  if (soon.length) {
    lines.push('', 'Coming up:');
    for (const d of soon) {
      const n = diffDays(date, d.dueAt);
      lines.push(`• ${d.title} (${d.kind}): ${n === 0 ? 'today' : n === 1 ? 'tomorrow' : `in ${n} days`}`);
    }
  }
  const risky = db.planMeta.health.deadlines.filter((h) => h.shortfallMin > 0);
  if (risky.length) {
    lines.push('', 'Needs attention:');
    for (const h of risky) lines.push(`• ${h.title}: ${fmtMin(h.shortfallMin)} doesn't fit. ${h.fixes[0] ?? ''}`);
  }
  const pending = db.inbox.filter((e) => !e.resolved).length;
  if (pending) lines.push('', `${pending} update${pending > 1 ? 's' : ''} from your connected apps to review in the Inbox.`);
  return { date, dayOff: !today.length, totalMin, subject, lines, text: lines.join('\n') };
}

export async function sendEmailDigest(db: DB, date?: ISODate): Promise<void> {
  const e = db.settings.email;
  if (!e?.host || !e.to) throw new Error('Email is not configured.');
  const d = buildDigest(db, date);
  const transport = nodemailer.createTransport({ host: e.host, port: e.port, secure: e.secure, auth: e.user ? { user: e.user, pass: e.pass } : undefined });
  await transport.sendMail({ from: e.from || e.user, to: e.to, subject: `📚 ${d.subject}`, text: `${d.text}\n\nOpen your plan: http://localhost:${process.env.PORT ?? 3000}/` });
}

/**
 * Once a day after the digest time: sync, re-plan, then email the digest.
 * Logged per date so it's sent at most once, and caught up at startup if missed.
 */
export function startDigestScheduler(): NodeJS.Timeout {
  let busy = false;
  const tick = async () => {
    if (busy) return;
    const db = load();
    const now = localParts(db.settings.timezone);
    if (db.planMeta.lastPlanDate !== now.date) {
      replan(db); // new day: roll missed sessions over
      save();
    }
    if (!db.settings.email?.host || now.minutes < hhmmToMin(db.settings.digestTime)) return;
    if (db.digestLog.some((l) => l.date === now.date && l.channel === 'email')) return;
    busy = true;
    try {
      await Promise.race([syncAll(), new Promise((r) => setTimeout(r, 60_000))]);
      replan(db);
      await sendEmailDigest(db, now.date);
      db.digestLog = [...db.digestLog, { date: now.date, channel: 'email' as const, sentAt: new Date().toISOString() }].slice(-60);
      save();
    } catch (err) {
      console.error('[digest] email failed:', (err as Error).message);
    } finally {
      busy = false;
    }
  };
  setTimeout(tick, 5_000);
  return setInterval(tick, 60_000);
}
