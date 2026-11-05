import type { ISODate, Source, SyncResult, UserState } from '../../shared/types';

/** Authorized Google API access for one user (handles token refresh). */
export interface GoogleApi {
  get(url: string): Promise<any>;
}

export interface ConnectorContext {
  source: Source;
  /** Read-only view of the user's data. */
  state: UserState;
  tz: string;
  today: ISODate;
  google?: GoogleApi;
}

export type Connector = (ctx: ConnectorContext) => Promise<SyncResult>;

/** Thrown when stored credentials no longer work and the user must reconnect. */
export class ReconnectError extends Error {}

/** Thrown for errors worth retrying later with backoff (rate limits, 5xx, network). */
export class TransientError extends Error {}
