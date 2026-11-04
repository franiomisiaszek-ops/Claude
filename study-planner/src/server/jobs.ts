/**
 * Background work: source syncs and daily reminders.
 * Safe with several server instances and with hosts that sleep: work is claimed with
 * FOR UPDATE SKIP LOCKED plus a lease, and anything overdue is picked up on the next tick.
 * A tick runs every minute in-process, and/or when an external scheduler calls /api/cron/tick.
 */
import { lt, sql } from 'drizzle-orm';
import type { DB } from './db';
import { aiUsage, inboxEntry } from './db/schema';
import { deliverDigest } from './digest';
import { syncSource } from './sync';

const rowsOf = (res: any): any[] => (Array.isArray(res) ? res : (res?.rows ?? []));

async function claimSources(db: DB, limit: number): Promise<{ id: string; user_id: string }[]> {
  const res = await db.execute(sql`
    update source set locked_until = now() + interval '10 minutes'
    where id in (
      select id from source
      where next_sync_at <= now() and needs_reconnect = false and (locked_until is null or locked_until < now())
      order by next_sync_at
      limit ${limit}
      for update skip locked
    )
    returning id, user_id`);
  return rowsOf(res);
}

async function claimDigests(db: DB, limit: number): Promise<{ user_id: string }[]> {
  const res = await db.execute(sql`
    update profile set next_digest_at = now() + interval '20 minutes'
    where user_id in (
      select user_id from profile
      where next_digest_at <= now()
      order by next_digest_at
      limit ${limit}
      for update skip locked
    )
    returning user_id`);
  return rowsOf(res);
}

async function pool<T>(items: T[], concurrency: number, fn: (x: T) => Promise<unknown>) {
  let i = 0;
  await Promise.all(
    Array.from({ length: Math.min(concurrency, items.length) }, async () => {
      while (i < items.length) {
        const item = items[i++];
        try {
          await fn(item);
        } catch (e) {
          console.error('[jobs]', (e as Error).message);
        }
      }
    }),
  );
}

let lastCleanup = 0;

export interface TickSummary {
  synced: number;
  digests: number;
  ms: number;
}

/** One round of due work, bounded so it fits inside a request time limit. */
export async function tick(db: DB, budgetMs = 25_000): Promise<TickSummary> {
  const start = Date.now();
  let synced = 0;
  let digests = 0;
  while (Date.now() - start < budgetMs) {
    const due = await claimDigests(db, 25);
    if (!due.length) break;
    await pool(due, 5, async (r) => {
      await deliverDigest(db, r.user_id);
      digests++;
    });
  }
  while (Date.now() - start < budgetMs) {
    const due = await claimSources(db, 10);
    if (!due.length) break;
    await pool(due, 4, async (r) => {
      await syncSource(db, r.user_id, r.id);
      synced++;
    });
  }
  if (Date.now() - lastCleanup > 6 * 3600_000) {
    lastCleanup = Date.now();
    const old = new Date(Date.now() - 60 * 86_400_000);
    await db.delete(inboxEntry).where(sql`${inboxEntry.resolved} = true and ${inboxEntry.createdAt} < ${old}`);
    await db.delete(aiUsage).where(lt(aiUsage.createdAt, new Date(Date.now() - 400 * 86_400_000)));
  }
  return { synced, digests, ms: Date.now() - start };
}

export function startJobs(db: DB): () => void {
  let running = false;
  const run = async () => {
    if (running) return;
    running = true;
    try {
      await tick(db, 50_000);
    } catch (e) {
      console.error('[jobs] tick failed:', (e as Error).message);
    } finally {
      running = false;
    }
  };
  const first = setTimeout(run, 5_000);
  const every = setInterval(run, 60_000);
  return () => {
    clearTimeout(first);
    clearInterval(every);
  };
}
