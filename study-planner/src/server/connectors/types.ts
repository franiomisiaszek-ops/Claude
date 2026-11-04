import type { DB, ISODate, Source, SyncResult } from '../../shared/types';

export interface ConnectorContext {
  source: Source;
  db: DB;
  tz: string;
  today: ISODate;
}

export type Connector = (ctx: ConnectorContext) => Promise<SyncResult>;

/** Thrown when stored credentials no longer work and the user must reconnect. */
export class ReconnectError extends Error {}

/** Thrown for errors worth retrying later with backoff (rate limits, 5xx, network). */
export class TransientError extends Error {}
