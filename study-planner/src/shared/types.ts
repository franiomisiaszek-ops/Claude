/** Calendar date in the user's timezone, `YYYY-MM-DD`. */
export type ISODate = string;

export type DeadlineKind = 'exam' | 'quiz' | 'assignment';
export type UnitType = 'pages' | 'slides' | 'problems' | 'videoMinutes' | 'topics' | 'minutes';
export type Difficulty = 1 | 2 | 3;
export type SessionStatus = 'planned' | 'done' | 'partial' | 'skipped' | 'missed';
export type SourceType = 'demo' | 'ics' | 'google-classroom' | 'google-calendar';

/** A grade as the user entered it, in their grading scale. */
export interface GradeValue {
  scale: string;
  value: number | string;
  /** For "points" scales: the maximum. */
  max?: number;
}

export interface Settings {
  onboarded: boolean;
  timezone: string;
  /** Local time for the daily reminder, `HH:MM`. */
  digestTime: string;
  /** Plan to this fraction of available time so small misses are absorbed. */
  slack: number;
  minChunk: number;
  maxSession: number;
  maxSubjects: number;
  /** The user's usual grading scale (see shared/grades.ts). */
  gradeScale: string;
  /** Default target grade for exams, in the user's scale. */
  targetGrade?: GradeValue;
  reminders: { push: boolean; email: boolean };
  /** Birth month `YYYY-MM`, asked once at setup (age gate; AI features are for adults). */
  birthMonth?: string;
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
  /** Reading-speed factor learned from logged session times. */
  calibrationFactor: number;
  samples: number;
  /** Target grade for this course (defaults to the user's). */
  targetGrade?: GradeValue;
  /** Grading scale used in this course (defaults to the user's). */
  gradeScale?: string;
  /** "Don't adjust this course from my grades." */
  learningLocked?: boolean;
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
  /**
   * Only when the user explicitly chose a guess because the material isn't known yet.
   * Without material and without this, the deadline gets no study time and asks for material.
   */
  roughMinutes?: number;
  /** `user`: a number the user typed; `history`: copied from a similar past exam. */
  roughSource?: 'user' | 'history';
  /** "More / less time for this exam" (1 = no change). Applies to this deadline only. */
  timeAdjust: number;
  targetGrade?: GradeValue;
}

export interface Material {
  id: string;
  courseId: string;
  deadlineId?: string;
  title: string;
  unitType: UnitType;
  quantity: number;
  /** How hard this is for the user: 1 easy, 2 normal, 3 hard. */
  difficulty: Difficulty;
  /** "More / less time on this" (1 = no change). Applies to this item only. */
  timeAdjust: number;
  estimateOverride?: number;
  order: number;
  availableFrom?: ISODate;
  done: boolean;
  origin?: Origin;
  userEdited: string[];
  link?: string;
  note?: string;
  /** Rated weak after an exam: gets a bigger share of practice time. */
  weak?: boolean;
}

export type ReasonCode =
  | 'deadline'
  | 'progress'
  | 'catchup'
  | 'bufferShrunk'
  | 'selfPaced'
  | 'review'
  | 'bufferReview'
  | 'pinned'
  | 'rough'
  | 'learned'
  | 'practice'
  | 'weak';

export interface Reason {
  code: ReasonCode;
  title?: string;
  kind?: DeadlineKind;
  daysLeft?: number;
  doneMin?: number;
  totalMin?: number;
  offset?: number;
  /** For `learned`: the multiplier applied from past results. */
  factor?: number;
}

export interface Session {
  id: string;
  date: ISODate;
  /** Material studied. For a rough-guess deadline this is `auto:<deadlineId>`. */
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
  /** Pace factor applied when this session was planned (for unbiased calibration). */
  pace?: number;
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
  /** No material and no rough guess: nothing can be planned until the user adds material. */
  needsMaterial?: boolean;
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
  /** The user action that triggered this re-plan (undoable), if any. */
  cause?: string;
}

export interface Source {
  id: string;
  type: SourceType;
  name: string;
  config: {
    url?: string;
    anchor?: ISODate;
    calendarIds?: string[];
    /** ICS only: an LMS deadline feed, or a personal calendar (non-study events count as busy time). */
    feedType?: 'lms' | 'calendar';
    /** Classroom only: per-course calendars Google creates, skipped by the Calendar connector. */
    courseCalendarIds?: string[];
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
  channel: 'email' | 'push';
  sentAt: string;
}

export interface Snapshot {
  id: string;
  createdAt: string;
  label: string;
  state: Pick<UserState, 'availability' | 'blocks' | 'courses' | 'deadlines' | 'materials' | 'sessions'>;
}

export type TopicRating = 'weak' | 'ok' | 'strong' | 'not_on_exam';

/** The post-exam debrief and (possibly later) grade, with the effort numbers at the exam date. */
export interface ExamResult {
  id: string;
  deadlineId: string;
  courseId: string;
  title: string;
  kind: DeadlineKind;
  dueAt: ISODate;
  /** Null until the grade is known. */
  grade?: GradeValue;
  /** Grade on a 0–1 scale; null until known. */
  score?: number;
  /** Target on a 0–1 scale at the time. */
  target: number;
  minutesInApp: number;
  minutesOutside: number;
  /** Material-based estimate (coverage, before learning and one-off adjustments). */
  baseEstimateMin: number;
  /** Where that estimate came from. A guess counts less in learning; a copy of past time is not learned from. */
  baseSource: 'material' | 'guess' | 'history';
  /** -1 easier than expected, 0 as expected, 1 harder. */
  feltDifficulty?: number;
  topicRatings: Record<string, TopicRating>;
  /** Left out of learning, e.g. "I was sick". */
  excluded: boolean;
  note?: string;
  gradeDueAt?: ISODate;
  reminderCount: number;
  recordedAt: string;
  gradedAt?: string;
}

export interface PlanMeta {
  lastPlanDate?: ISODate;
  health: Health;
  diff?: PlanDiff;
  plannedAt?: string;
}

/** Everything that belongs to one user. Loaded, changed in memory, then saved as a diff. */
export interface UserState {
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
  results: ExamResult[];
  planMeta: PlanMeta;
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
  /** Free text from the source (e.g. a Classroom description), used for AI scope extraction. */
  description?: string;
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
  /** Changes to the source's own config learned during sync. */
  configPatch?: Partial<Source['config']>;
}

export interface PublicUser {
  id: string;
  name: string;
  email: string;
  emailVerified: boolean;
  image?: string | null;
}

/** What the browser receives for the signed-in user. */
export type PublicState = Omit<UserState, 'snapshots'> & {
  canUndo: string | null;
  today: ISODate;
  user: PublicUser;
  google: { available: boolean; connected: boolean; email?: string | null };
  features: { ai: boolean; push: boolean; email: boolean };
  /** The operator (ADMIN_EMAILS) sees an owner status card. */
  isAdmin: boolean;
  vapidPublicKey?: string;
  ai: { usedToday: number; dailyLimit: number };
};
