import { PGlite } from '@electric-sql/pglite';
import { sql } from 'drizzle-orm';
import { drizzle as drizzlePg } from 'drizzle-orm/node-postgres';
import { migrate as migratePg } from 'drizzle-orm/node-postgres/migrator';
import type { PgDatabase, PgQueryResultHKT } from 'drizzle-orm/pg-core';
import { drizzle as drizzlePglite } from 'drizzle-orm/pglite';
import { migrate as migratePglite } from 'drizzle-orm/pglite/migrator';
import fs from 'node:fs';
import path from 'node:path';
import pg from 'pg';
import * as schema from './schema';

export type DB = PgDatabase<PgQueryResultHKT, typeof schema>;
export { schema };

const MIGRATIONS = path.resolve(process.env.MIGRATIONS_DIR ?? 'drizzle');

export interface Connection {
  db: DB;
  kind: 'postgres' | 'pglite';
  close: () => Promise<void>;
}

/**
 * Connects and runs migrations.
 * - With a URL: real Postgres (e.g. Supabase). Use the "session pooler" connection string.
 * - Without: embedded Postgres (PGlite) in `dataDir`, or in memory when `dataDir` is ':memory:'.
 */
export async function connect(opts: { url?: string; dataDir?: string }): Promise<Connection> {
  if (opts.url) {
    const ssl =
      process.env.DATABASE_SSL === 'disable'
        ? false
        : process.env.DATABASE_CA_CERT
          ? { ca: process.env.DATABASE_CA_CERT }
          : /localhost|127\.0\.0\.1/.test(opts.url)
            ? false
            : { rejectUnauthorized: false };
    const pool = new pg.Pool({ connectionString: opts.url, max: Number(process.env.DATABASE_POOL_MAX ?? 5), ssl, idleTimeoutMillis: 30_000 });
    const db = drizzlePg(pool, { schema }) as unknown as DB;
    await migratePg(db as any, { migrationsFolder: MIGRATIONS });
    return { db, kind: 'postgres', close: () => pool.end() };
  }
  const dir = opts.dataDir ?? 'data';
  let client: PGlite;
  if (dir === ':memory:') client = new PGlite();
  else {
    fs.mkdirSync(dir, { recursive: true });
    client = new PGlite(path.join(dir, 'pglite'));
  }
  const db = drizzlePglite(client, { schema }) as unknown as DB;
  await migratePglite(db as any, { migrationsFolder: MIGRATIONS });
  return { db, kind: 'pglite', close: () => client.close() };
}

/** Serializes all writes for one user within a transaction (works across server instances). */
export async function withUserLock<T>(db: DB, userId: string, fn: (tx: DB) => Promise<T>): Promise<T> {
  return db.transaction(async (tx) => {
    await tx.execute(sql`select pg_advisory_xact_lock(hashtext(${userId}))`);
    return fn(tx as unknown as DB);
  });
}
