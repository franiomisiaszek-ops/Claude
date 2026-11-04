import { eq } from 'drizzle-orm';
import { APP_NAME } from '../shared/brand';
import { addDays, diffDays, fmtDate, fmtMin } from '../shared/dates';
import { sessionTitle } from '../shared/labels';
import type { ISODate, UserState } from '../shared/types';
import { withUserLock, type DB } from './db';
import { profile, user } from './db/schema';
import { sendEmail } from './email';
import { env, features } from './env';
import { replan, todayFor } from './plan-service';
import { sendPush } from './push';
import { loadState, nextDigestAt, saveState } from './repo';

export { sessionTitle };

export interface Digest {
  date: ISODate;
  dayOff: boolean;
  totalMin: number;
  subject: string;
  /** Short text for a notification. */
  short: string;
  lines: string[];
  text: string;
}

/** The daily message: what to study today and for how long, or that it's a day off. */
export function buildDigest(state: UserState, date: ISODate = todayFor(state)): Digest {
  const today = state.sessions.filter((s) => s.date === date && s.status === 'planned');
  const totalMin = today.reduce((a, s) => a + s.minutes, 0);
  const lines: string[] = [];
  let subject: string;
  let short: string;
  if (!today.length) {
    const next = state.sessions.find((s) => s.date > date && s.status === 'planned');
    const blocked = state.blocks.some((b) => b.start <= date && date <= b.end);
    subject = blocked ? 'Day off today: you blocked this day' : 'Day off today';
    short = next ? `Nothing to study today. Next: ${fmtDate(next.date)}, ${sessionTitle(state, next)}.` : 'Nothing to study today.';
    lines.push(short);
  } else {
    subject = `Today: ${fmtMin(totalMin)} of study`;
    for (const s of today) lines.push(`• ${sessionTitle(state, s)}: ${fmtMin(s.minutes)}`);
    short = today
      .slice(0, 3)
      .map((s) => `${sessionTitle(state, s)} (${fmtMin(s.minutes)})`)
      .join(' · ');
    if (today.length > 3) short += ` +${today.length - 3} more`;
  }
  const soon = state.deadlines
    .filter((d) => d.status === 'active' && d.dueAt >= date && d.dueAt <= addDays(date, 7))
    .sort((a, b) => a.dueAt.localeCompare(b.dueAt));
  if (soon.length) {
    lines.push('', 'Coming up:');
    for (const d of soon) {
      const n = diffDays(date, d.dueAt);
      lines.push(`• ${d.title} (${d.kind}): ${n === 0 ? 'today' : n === 1 ? 'tomorrow' : `in ${n} days`}`);
    }
  }
  const needsMaterial = state.planMeta.health.deadlines.filter((h) => h.needsMaterial);
  if (needsMaterial.length) {
    lines.push('', 'Add material so these can be planned:');
    for (const h of needsMaterial) lines.push(`• ${h.title}`);
  }
  const risky = state.planMeta.health.deadlines.filter((h) => h.shortfallMin > 0);
  if (risky.length) {
    lines.push('', 'Needs attention:');
    for (const h of risky) lines.push(`• ${h.title}: ${fmtMin(h.shortfallMin)} doesn't fit. ${h.fixes[0] ?? ''}`);
  }
  const awaitingResult = pendingResults(state, date);
  if (awaitingResult.length) lines.push('', `How did ${awaitingResult[0].title} go? Add your grade so future plans fit you better.`);
  const pending = state.inbox.filter((e) => !e.resolved).length;
  if (pending) lines.push('', `${pending} update${pending > 1 ? 's' : ''} from your connected apps to review.`);
  return { date, dayOff: !today.length, totalMin, subject, short, lines, text: lines.join('\n') };
}

/** Exams and quizzes that are over but have no result yet (last 21 days). */
export function pendingResults(state: UserState, today: ISODate) {
  const have = new Set(state.results.map((r) => r.deadlineId));
  return state.deadlines.filter((d) => d.kind !== 'assignment' && d.dueAt <= today && d.dueAt >= addDays(today, -21) && !have.has(d.id));
}

/** Whether a reminder is worth sending: something planned or due in the next two weeks. */
function hasActivity(state: UserState, date: ISODate): boolean {
  const horizon = addDays(date, 14);
  return (
    state.sessions.some((s) => s.status === 'planned' && s.date >= date && s.date <= horizon) ||
    state.deadlines.some((d) => d.status === 'active' && d.dueAt >= date && d.dueAt <= horizon) ||
    pendingResults(state, date).length > 0
  );
}

/** Builds and sends today's reminder for one user. Idempotent per date and channel. */
export async function deliverDigest(db: DB, userId: string): Promise<{ push: number; email: boolean }> {
  const { state, digest, date } = await withUserLock(db, userId, async (tx) => {
    const before = await loadState(tx, userId);
    const after = structuredClone(before);
    const date = todayFor(after);
    if (after.planMeta.lastPlanDate !== date) replan(after);
    await saveState(tx, userId, before, after);
    return { state: after, digest: buildDigest(after, date), date };
  });
  const outcome = { push: 0, email: false };
  if (state.settings.onboarded && hasActivity(state, date)) {
    const already = new Set(state.digestLog.filter((l) => l.date === date).map((l) => l.channel));
    if (state.settings.reminders.push && !already.has('push')) {
      outcome.push = await sendPush(db, userId, { title: `📚 ${digest.subject}`, body: digest.short, url: '/', tag: `digest-${date}` });
    }
    if (state.settings.reminders.email && features.email() && !already.has('email')) {
      const [u] = await db.select({ email: user.email, verified: user.emailVerified }).from(user).where(eq(user.id, userId));
      if (u?.verified) {
        await sendEmail({ to: u.email, subject: `📚 ${digest.subject}`, text: `${digest.text}\n\nOpen ${APP_NAME}: ${env.publicUrl}/\n\nTurn these emails off in Settings.` });
        outcome.email = true;
      }
    }
    if (outcome.push || outcome.email) {
      await withUserLock(db, userId, async (tx) => {
        const before = await loadState(tx, userId);
        const after = structuredClone(before);
        const sentAt = new Date().toISOString();
        if (outcome.push) after.digestLog.push({ date, channel: 'push', sentAt });
        if (outcome.email) after.digestLog.push({ date, channel: 'email', sentAt });
        after.digestLog = after.digestLog.filter((l) => l.date >= addDays(date, -14));
        await saveState(tx, userId, before, after);
      });
    }
  }
  await db.update(profile).set({ nextDigestAt: nextDigestAt(state.settings) }).where(eq(profile.userId, userId));
  return outcome;
}
