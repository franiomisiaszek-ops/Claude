/**
 * Database schema (Postgres). Every app table is scoped by `userId` and cascades on account deletion.
 * Auth tables follow Better Auth's core schema; JS keys match Better Auth field names.
 */
import { sql } from 'drizzle-orm';
import { bigint, boolean, date, index, integer, jsonb, pgTable, primaryKey, real, text, timestamp, uniqueIndex } from 'drizzle-orm/pg-core';

const createdAt = () => timestamp('created_at', { withTimezone: true }).notNull().defaultNow();
const updatedAt = () => timestamp('updated_at', { withTimezone: true }).notNull().defaultNow();

// ---------- Auth (Better Auth) ----------

export const user = pgTable('user', {
  id: text('id').primaryKey(),
  name: text('name').notNull(),
  email: text('email').notNull().unique(),
  emailVerified: boolean('email_verified').notNull().default(false),
  image: text('image'),
  createdAt: createdAt(),
  updatedAt: updatedAt(),
});

export const session = pgTable(
  'session',
  {
    id: text('id').primaryKey(),
    expiresAt: timestamp('expires_at', { withTimezone: true }).notNull(),
    token: text('token').notNull().unique(),
    createdAt: createdAt(),
    updatedAt: updatedAt(),
    ipAddress: text('ip_address'),
    userAgent: text('user_agent'),
    userId: text('user_id')
      .notNull()
      .references(() => user.id, { onDelete: 'cascade' }),
  },
  (t) => [index('session_user_idx').on(t.userId)],
);

export const account = pgTable(
  'account',
  {
    id: text('id').primaryKey(),
    accountId: text('account_id').notNull(),
    providerId: text('provider_id').notNull(),
    userId: text('user_id')
      .notNull()
      .references(() => user.id, { onDelete: 'cascade' }),
    accessToken: text('access_token'),
    refreshToken: text('refresh_token'),
    idToken: text('id_token'),
    accessTokenExpiresAt: timestamp('access_token_expires_at', { withTimezone: true }),
    refreshTokenExpiresAt: timestamp('refresh_token_expires_at', { withTimezone: true }),
    scope: text('scope'),
    password: text('password'),
    createdAt: createdAt(),
    updatedAt: updatedAt(),
  },
  (t) => [index('account_user_idx').on(t.userId)],
);

export const verification = pgTable(
  'verification',
  {
    id: text('id').primaryKey(),
    identifier: text('identifier').notNull(),
    value: text('value').notNull(),
    expiresAt: timestamp('expires_at', { withTimezone: true }).notNull(),
    createdAt: createdAt(),
    updatedAt: updatedAt(),
  },
  (t) => [index('verification_identifier_idx').on(t.identifier)],
);

export const rateLimit = pgTable('rate_limit', {
  id: text('id').primaryKey(),
  key: text('key').notNull().unique(),
  count: integer('count').notNull(),
  lastRequest: bigint('last_request', { mode: 'number' }).notNull(),
});

// ---------- App ----------

const owner = () =>
  text('user_id')
    .notNull()
    .references(() => user.id, { onDelete: 'cascade' });

/** One row per user: settings, availability and plan metadata. */
export const profile = pgTable(
  'profile',
  {
    userId: text('user_id')
      .primaryKey()
      .references(() => user.id, { onDelete: 'cascade' }),
    settings: jsonb('settings').notNull(),
    availability: jsonb('availability').notNull(),
    planMeta: jsonb('plan_meta').notNull(),
    /** Next time (UTC) the daily reminder is due; drives the reminder job. */
    nextDigestAt: timestamp('next_digest_at', { withTimezone: true }),
    createdAt: createdAt(),
    updatedAt: updatedAt(),
  },
  (t) => [index('profile_next_digest_idx').on(t.nextDigestAt)],
);

export const course = pgTable(
  'course',
  {
    id: text('id').primaryKey(),
    userId: owner(),
    name: text('name').notNull(),
    color: text('color').notNull(),
    calibrationFactor: real('calibration_factor').notNull().default(1),
    samples: integer('samples').notNull().default(0),
    targetGrade: jsonb('target_grade'),
    /** Grading scale used in this course (defaults to the user's). */
    gradeScale: text('grade_scale'),
    /** "Don't adjust this course from my grades." */
    learningLocked: boolean('learning_locked').notNull().default(false),
  },
  (t) => [index('course_user_idx').on(t.userId)],
);

export const deadline = pgTable(
  'deadline',
  {
    id: text('id').primaryKey(),
    userId: owner(),
    courseId: text('course_id').notNull(),
    kind: text('kind').notNull(),
    title: text('title').notNull(),
    dueAt: date('due_at', { mode: 'string' }).notNull(),
    status: text('status').notNull(),
    origin: jsonb('origin'),
    userEdited: jsonb('user_edited').notNull().default(sql`'[]'::jsonb`),
    confidence: real('confidence').notNull().default(1),
    /** A study-time guess the user explicitly chose while material is unknown. */
    roughMinutes: integer('rough_minutes'),
    /** Where the guess came from: the user's own number, or their history. */
    roughSource: text('rough_source'),
    /** "More / less time for this exam", applies to this deadline only. */
    timeAdjust: real('time_adjust').notNull().default(1),
    targetGrade: jsonb('target_grade'),
    createdAt: createdAt(),
  },
  (t) => [index('deadline_user_idx').on(t.userId)],
);

export const material = pgTable(
  'material',
  {
    id: text('id').primaryKey(),
    userId: owner(),
    courseId: text('course_id').notNull(),
    deadlineId: text('deadline_id'),
    title: text('title').notNull(),
    unitType: text('unit_type').notNull(),
    quantity: real('quantity').notNull(),
    difficulty: integer('difficulty').notNull(),
    /** The user's own "more / less time on this" multiplier. */
    timeAdjust: real('time_adjust').notNull().default(1),
    estimateOverride: integer('estimate_override'),
    order: integer('sort_order').notNull().default(0),
    availableFrom: date('available_from', { mode: 'string' }),
    done: boolean('done').notNull().default(false),
    origin: jsonb('origin'),
    userEdited: jsonb('user_edited').notNull().default(sql`'[]'::jsonb`),
    link: text('link'),
    note: text('note'),
    /** Rated weak after an exam: gets a bigger share of practice time. */
    weak: boolean('weak').notNull().default(false),
  },
  (t) => [index('material_user_idx').on(t.userId)],
);

export const studySession = pgTable(
  'study_session',
  {
    id: text('id').notNull(),
    userId: owner(),
    date: date('date', { mode: 'string' }).notNull(),
    materialId: text('material_id').notNull(),
    reviewMaterialIds: jsonb('review_material_ids'),
    deadlineId: text('deadline_id'),
    courseId: text('course_id').notNull(),
    kind: text('kind').notNull(),
    minutes: integer('minutes').notNull(),
    pinned: boolean('pinned').notNull().default(false),
    status: text('status').notNull(),
    actualMinutes: integer('actual_minutes'),
    reasons: jsonb('reasons').notNull().default(sql`'[]'::jsonb`),
  },
  (t) => [primaryKey({ columns: [t.userId, t.id] }), index('session_user_date_idx').on(t.userId, t.date)],
);

export const block = pgTable(
  'block',
  {
    id: text('id').primaryKey(),
    userId: owner(),
    start: date('start_date', { mode: 'string' }).notNull(),
    end: date('end_date', { mode: 'string' }).notNull(),
    reason: text('reason'),
  },
  (t) => [index('block_user_idx').on(t.userId)],
);

export const busyEvent = pgTable(
  'busy_event',
  {
    id: text('id').primaryKey(),
    userId: owner(),
    sourceId: text('source_id').notNull(),
    date: date('date', { mode: 'string' }).notNull(),
    startMin: integer('start_min').notNull(),
    endMin: integer('end_min').notNull(),
    title: text('title'),
  },
  (t) => [index('busy_user_idx').on(t.userId)],
);

export const source = pgTable(
  'source',
  {
    id: text('id').primaryKey(),
    userId: owner(),
    type: text('type').notNull(),
    name: text('name').notNull(),
    config: jsonb('config').notNull(),
    status: text('status').notNull(),
    intervalMin: integer('interval_min').notNull(),
    lastSyncAt: timestamp('last_sync_at', { withTimezone: true }),
    lastChangeAt: timestamp('last_change_at', { withTimezone: true }),
    etag: text('etag'),
    lastModified: text('last_modified'),
    error: text('error'),
    errorCount: integer('error_count').notNull().default(0),
    needsReconnect: boolean('needs_reconnect').notNull().default(false),
    /** When the background job should sync this source next. */
    nextSyncAt: timestamp('next_sync_at', { withTimezone: true }).notNull().defaultNow(),
    /** Lease so two workers never sync the same source at once. */
    lockedUntil: timestamp('locked_until', { withTimezone: true }),
  },
  (t) => [index('source_user_idx').on(t.userId), index('source_next_sync_idx').on(t.nextSyncAt)],
);

export const inboxEntry = pgTable(
  'inbox_entry',
  {
    id: text('id').primaryKey(),
    userId: owner(),
    change: text('change').notNull(),
    entity: text('entity').notNull(),
    refId: text('ref_id'),
    sourceId: text('source_id').notNull(),
    externalId: text('external_id').notNull(),
    title: text('title').notNull(),
    payload: jsonb('payload').notNull(),
    createdAt: timestamp('created_at', { withTimezone: true }).notNull().defaultNow(),
    resolved: boolean('resolved').notNull().default(false),
  },
  (t) => [index('inbox_user_idx').on(t.userId)],
);

export const snapshot = pgTable(
  'snapshot',
  {
    id: text('id').primaryKey(),
    userId: owner(),
    createdAt: timestamp('created_at', { withTimezone: true }).notNull().defaultNow(),
    label: text('label').notNull(),
    state: jsonb('state').notNull(),
  },
  (t) => [index('snapshot_user_idx').on(t.userId)],
);

export const digestLog = pgTable(
  'digest_log',
  {
    userId: owner(),
    date: date('date', { mode: 'string' }).notNull(),
    channel: text('channel').notNull(),
    sentAt: timestamp('sent_at', { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [primaryKey({ columns: [t.userId, t.date, t.channel] })],
);

/** Google Classroom/Calendar connection. Tokens are AES-256-GCM encrypted. */
export const googleConnection = pgTable('google_connection', {
  userId: text('user_id')
    .primaryKey()
    .references(() => user.id, { onDelete: 'cascade' }),
  googleEmail: text('google_email'),
  refreshTokenEnc: text('refresh_token_enc').notNull(),
  accessTokenEnc: text('access_token_enc'),
  expiresAt: timestamp('expires_at', { withTimezone: true }),
  scope: text('scope'),
  createdAt: createdAt(),
  updatedAt: updatedAt(),
});

export const pushSubscription = pgTable(
  'push_subscription',
  {
    id: text('id').primaryKey(),
    userId: owner(),
    endpoint: text('endpoint').notNull(),
    p256dh: text('p256dh').notNull(),
    auth: text('auth').notNull(),
    userAgent: text('user_agent'),
    createdAt: createdAt(),
    lastSuccessAt: timestamp('last_success_at', { withTimezone: true }),
    failureCount: integer('failure_count').notNull().default(0),
  },
  (t) => [index('push_user_idx').on(t.userId), uniqueIndex('push_endpoint_idx').on(t.endpoint)],
);

/** The post-exam debrief and (possibly later) grade, plus the effort numbers at the exam date. */
export const examResult = pgTable(
  'exam_result',
  {
    id: text('id').primaryKey(),
    userId: owner(),
    deadlineId: text('deadline_id').notNull(),
    courseId: text('course_id').notNull(),
    title: text('title').notNull(),
    kind: text('kind').notNull(),
    dueAt: date('due_at', { mode: 'string' }).notNull(),
    /** Grade as entered (scale + value); null until the grade is known. */
    grade: jsonb('grade'),
    /** Grade on a 0–1 scale; null until known. */
    score: real('score'),
    /** Target on a 0–1 scale at the time. */
    target: real('target').notNull(),
    minutesInApp: integer('minutes_in_app').notNull(),
    minutesOutside: integer('minutes_outside').notNull().default(0),
    /** Material-based estimate (coverage, before learning and one-off adjustments). */
    baseEstimateMin: integer('base_estimate_min').notNull(),
    /** How the exam felt: -1 easier than expected, 0 as expected, 1 harder. */
    feltDifficulty: integer('felt_difficulty'),
    /** Per material: weak | ok | strong | not_on_exam. */
    topicRatings: jsonb('topic_ratings').notNull().default(sql`'{}'::jsonb`),
    /** Excluded from learning, e.g. "I was sick". */
    excluded: boolean('excluded').notNull().default(false),
    note: text('note'),
    /** When the grade is expected, for reminders. */
    gradeDueAt: date('grade_due_at', { mode: 'string' }),
    reminderCount: integer('reminder_count').notNull().default(0),
    recordedAt: timestamp('recorded_at', { withTimezone: true }).notNull().defaultNow(),
    gradedAt: timestamp('graded_at', { withTimezone: true }),
  },
  (t) => [index('result_user_idx').on(t.userId), uniqueIndex('result_deadline_idx').on(t.userId, t.deadlineId)],
);

/** Claude API usage per user: quotas, cost tracking and result caching. */
export const aiUsage = pgTable(
  'ai_usage',
  {
    id: text('id').primaryKey(),
    userId: owner(),
    kind: text('kind').notNull(),
    cacheKey: text('cache_key').notNull(),
    model: text('model').notNull(),
    inputTokens: integer('input_tokens').notNull().default(0),
    outputTokens: integer('output_tokens').notNull().default(0),
    costMicros: integer('cost_micros').notNull().default(0),
    result: jsonb('result'),
    createdAt: createdAt(),
  },
  (t) => [index('ai_usage_user_idx').on(t.userId, t.createdAt), index('ai_usage_cache_idx').on(t.userId, t.cacheKey)],
);

/** Server-generated secrets (e.g. push keys), encrypted with TOKEN_ENCRYPTION_KEY. */
export const appSecret = pgTable('app_secret', {
  key: text('key').primaryKey(),
  valueEnc: text('value_enc').notNull(),
  createdAt: createdAt(),
});
