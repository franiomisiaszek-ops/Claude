/**
 * Runs a connector for one user's source and merges the result.
 * The network call happens outside any lock; the merge + re-plan + save happen under the user lock.
 */
import { eq } from 'drizzle-orm';
import type { Source, SyncResult } from '../shared/types';
import { syncDemo } from './connectors/demo';
import { googleApi, syncCalendar, syncClassroom } from './connectors/google';
import { syncIcs } from './connectors/ics';
import { ReconnectError, type Connector } from './connectors/types';
import { withUserLock, type DB } from './db';
import * as s from './db/schema';
import { merge } from './merge';
import { replan, todayFor } from './plan-service';
import { loadState, saveState } from './repo';

const CONNECTORS: Record<Source['type'], Connector> = {
  demo: syncDemo,
  ics: syncIcs,
  'google-classroom': syncClassroom,
  'google-calendar': syncCalendar,
};

export interface SyncOutcome {
  changed: boolean;
  error?: string;
}

export async function syncSource(db: DB, userId: string, sourceId: string): Promise<SyncOutcome> {
  const snapshot = await loadState(db, userId);
  const src = snapshot.sources.find((x) => x.id === sourceId);
  if (!src) return { changed: false };
  let result: SyncResult | null = null;
  let error: Error | null = null;
  try {
    const google = src.type.startsWith('google') ? await googleApi(db, userId) : undefined;
    result = await CONNECTORS[src.type]({ source: src, state: snapshot, tz: snapshot.settings.timezone, today: todayFor(snapshot), google });
  } catch (e) {
    error = e as Error;
  }

  return withUserLock(db, userId, async (tx) => {
    const before = await loadState(tx, userId);
    const after = structuredClone(before);
    const source = after.sources.find((x) => x.id === sourceId);
    if (!source) return { changed: false };
    const now = new Date().toISOString();
    let changed = false;
    if (result) {
      if (result.configPatch) source.config = { ...source.config, ...result.configPatch };
      changed = merge(after, source, result);
      source.status = 'ok';
      source.error = undefined;
      source.errorCount = 0;
      source.needsReconnect = false;
      source.lastSyncAt = now;
      source.etag = result.etag ?? source.etag;
      source.lastModified = result.lastModified ?? source.lastModified;
      if (changed) {
        source.lastChangeAt = now;
        replan(after);
      }
    } else if (error) {
      source.status = 'error';
      source.error = error.message.slice(0, 300);
      source.errorCount = (source.errorCount ?? 0) + 1;
      source.lastSyncAt = now;
      if (error instanceof ReconnectError) for (const x of after.sources) if (x.type.startsWith('google')) x.needsReconnect = true;
    }
    await saveState(tx, userId, before, after);
    const backoff = result ? 1 : 2 ** Math.min(source.errorCount, 4);
    await tx
      .update(s.source)
      .set({ nextSyncAt: new Date(Date.now() + source.intervalMin * 60_000 * backoff), lockedUntil: null })
      .where(eq(s.source.id, sourceId));
    return { changed, error: error?.message };
  });
}

/** Syncs all of a user's sources, Classroom first (so Calendar knows which course calendars to skip). */
export async function syncAllForUser(db: DB, userId: string): Promise<SyncOutcome[]> {
  const state = await loadState(db, userId);
  const order = [...state.sources].sort((a, b) => Number(b.type === 'google-classroom') - Number(a.type === 'google-classroom'));
  const out: SyncOutcome[] = [];
  for (const src of order) if (!src.needsReconnect) out.push(await syncSource(db, userId, src.id));
  return out;
}
