/** Calendar date in the user's timezone, `YYYY-MM-DD`. */
export type ISODate = string;

export type DeadlineKind = 'exam' | 'quiz' | 'assignment';
export type UnitType = 'pages' | 'slides' | 'problems' | 'videoMinutes' | 'topics' | 'minutes';
export type Difficulty = 1 | 2 | 3;
export type SessionStatus = 'planned' | 'done' | 'partial' | 'skipped' | 'missed';
export type SourceType = 'demo' | 'ics' | 'google-classroom' | 'google-calendar';

export interface EmailSettings {
  host: string;
  port: number;
  secure: boolean;
  user: string;
  pass: string;
  from: string;
  to: string;
}

export interface Settings {
  onboarded: boolean;
  timezone: string;
  /** Local time for the daily digest, `HH:MM`. */
  digestTime: string;
  /** Plan to this fraction of available time so small misses are absorbed. */
  slack: number;
  minChunk: number;
  maxSession: number;
  maxSubjects: number;
  email?: EmailSettings;
  googleClient?: { clientId: string; clientSecret: string };
}

export interface Availability {
  /** Study minutes per weekday, index 0 = Sunday. */
  minutesByWeekday: number[];
  /** Optional local study window; when set, busy calendar events inside it reduce capacity. */
  studyWindow?: { start: string; end: string };
}

export interface Block {
  id: string;
  start: ISODate;
  end: ISODate;
  reason?: string;
}

/** Busy time already normalised to a local date and minutes-from-midnight. */
export interface BusyEvent {
  sourceId: string;
  date: ISODate;
  startMin: number;
  endMin: number;
  title?: string;
}

/** Where an imported item came from, plus the last values that source reported. */
export interface Origin {
  sourceId: string;
  externalId: string;
  rawHash: string;
  fields: Record<string, unknown>;
  missingCount?: number;
}

export interface Course {
  id: string;
  name: string;
  color: string;
  calibrationFactor: number;
  samples: number;
}

export interface Deadline {
  id: string;
  courseId: string;
  kind: DeadlineKind;
  title: string;
  dueAt: ISODate;
  status: 'active' | 'done';
  origin?: Origin;
  /** Fields the user changed by hand; sync never overwrites these silently. */
  userEdited: string[];
  confidence: number;
}

export interface Material {
  id: string;
  courseId: string;
  deadlineId?: string;
  title: string;
  unitType: UnitType;
  quantity: number;
  difficulty: Difficulty;
  estimateOverride?: number;
  order: number;
  availableFrom?: ISODate;
  done: boolean;
  origin?: Origin;
  userEdited: string[];
  link?: string;
}

export type ReasonCode =
  | 'deadline'
  | 'progress'
  | 'catchup'
  | 'bufferShrunk'
  | 'selfPaced'
  | 'review'
  | 'bufferReview'
  | 'pinned';

export interface Reason {
  code: ReasonCode;
  title?: string;
  kind?: DeadlineKind;
  daysLeft?: number;
  doneMin?: number;
  totalMin?: number;
  offset?: number;
}

export interface Session {
  id: string;
  date: ISODate;
  /** Material studied. For a deadline with no materials this is `auto:<deadlineId>`. */
  materialId: string;
  /** For combined review sessions, every material covered. */
  reviewMaterialIds?: string[];
  deadlineId?: string;
  courseId: string;
  kind: 'new' | 'review';
  minutes: number;
  pinned: boolean;
  status: SessionStatus;
  actualMinutes?: number;
  reasons: Reason[];
}

export interface DeadlineHealth {
  deadlineId: string;
  title: string;
  dueAt: ISODate;
  totalMin: number;
  doneMin: number;
  plannedMin: number;
  shortfallMin: number;
  bufferShrunk: boolean;
  fixes: string[];
}

export interface Health {
  status: 'ok' | 'tight' | 'atRisk';
  deadlines: DeadlineHealth[];
}

export interface PlanDiff {
  added: number;
  removed: number;
  changed: number;
  summary: string;
}

export interface Source {
  id: string;
  type: SourceType;
  name: string;
  config: {
    url?: string;
    anchor?: ISODate;
    calendarIds?: string[];
    busyFromOtherEvents?: boolean;
  };
  status: 'never' | 'ok' | 'error' | 'syncing';
  intervalMin: number;
  lastSyncAt?: string;
  lastChangeAt?: string;
  etag?: string;
  lastModified?: string;
  error?: string;
  errorCount: number;
  needsReconnect?: boolean;
}

export interface InboxEntry {
  id: string;
  change: 'new' | 'changed' | 'removed' | 'conflict';
  entity: 'deadline' | 'material';
  refId?: string;
  sourceId: string;
  externalId: string;
  title: string;
  /** For new items: the proposed item. For conflicts: the field and both values. */
  payload: Record<string, any>;
  createdAt: string;
  resolved: boolean;
}

export interface DigestLog {
  date: ISODate;
  channel: 'email' | 'browser';
  sentAt: string;
}

export interface Snapshot {
  createdAt: string;
  label: string;
  state: Pick<DB, 'availability' | 'blocks' | 'courses' | 'deadlines' | 'materials' | 'sessions'>;
}

export interface DB {
  version: 1;
  settings: Settings;
  availability: Availability;
  blocks: Block[];
  courses: Course[];
  deadlines: Deadline[];
  materials: Material[];
  sessions: Session[];
  busy: BusyEvent[];
  sources: Source[];
  inbox: InboxEntry[];
  snapshots: Snapshot[];
  digestLog: DigestLog[];
  planMeta: { lastPlanDate?: ISODate; health: Health; diff?: PlanDiff; plannedAt?: string };
}

/** Normalised item a connector reports. */
export interface SourceItem {
  externalId: string;
  kind: DeadlineKind | 'material';
  confidence: number;
  title: string;
  courseName: string;
  dueAt?: ISODate;
  done?: boolean;
  unitType?: UnitType;
  quantity?: number;
  /** externalId of the deadline this material prepares for, from the same source. */
  deadlineExternalId?: string;
  link?: string;
}

export interface SyncResult {
  items: SourceItem[];
  busy?: Omit<BusyEvent, 'sourceId'>[];
  /** True when `items` is every item the source currently has (enables removal detection). */
  complete: boolean;
  /** For windowed feeds, only items whose date falls inside this range can be treated as removed. */
  range?: { start: ISODate; end: ISODate };
  notModified?: boolean;
  etag?: string;
  lastModified?: string;
}

/** What the browser receives: the DB with secrets removed. */
export type PublicState = Omit<DB, 'settings' | 'snapshots'> & {
  settings: Omit<Settings, 'email' | 'googleClient'> & {
    email?: Omit<EmailSettings, 'pass'> & { hasPass: boolean };
    googleClient?: { clientId: string; hasSecret: boolean };
  };
  canUndo: string | null;
  googleConnected: boolean;
  redirectUri: string;
  today: ISODate;
};
