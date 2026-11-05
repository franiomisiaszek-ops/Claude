CREATE TABLE "account" (
	"id" text PRIMARY KEY NOT NULL,
	"account_id" text NOT NULL,
	"provider_id" text NOT NULL,
	"user_id" text NOT NULL,
	"access_token" text,
	"refresh_token" text,
	"id_token" text,
	"access_token_expires_at" timestamp with time zone,
	"refresh_token_expires_at" timestamp with time zone,
	"scope" text,
	"password" text,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "ai_usage" (
	"id" text PRIMARY KEY NOT NULL,
	"user_id" text NOT NULL,
	"kind" text NOT NULL,
	"cache_key" text NOT NULL,
	"model" text NOT NULL,
	"input_tokens" integer DEFAULT 0 NOT NULL,
	"output_tokens" integer DEFAULT 0 NOT NULL,
	"cost_micros" integer DEFAULT 0 NOT NULL,
	"result" jsonb,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "block" (
	"id" text PRIMARY KEY NOT NULL,
	"user_id" text NOT NULL,
	"start_date" date NOT NULL,
	"end_date" date NOT NULL,
	"reason" text
);
--> statement-breakpoint
CREATE TABLE "busy_event" (
	"id" text PRIMARY KEY NOT NULL,
	"user_id" text NOT NULL,
	"source_id" text NOT NULL,
	"date" date NOT NULL,
	"start_min" integer NOT NULL,
	"end_min" integer NOT NULL,
	"title" text
);
--> statement-breakpoint
CREATE TABLE "course" (
	"id" text PRIMARY KEY NOT NULL,
	"user_id" text NOT NULL,
	"name" text NOT NULL,
	"color" text NOT NULL,
	"calibration_factor" real DEFAULT 1 NOT NULL,
	"samples" integer DEFAULT 0 NOT NULL,
	"target_grade" jsonb,
	"grade_scale" text,
	"learning_locked" boolean DEFAULT false NOT NULL
);
--> statement-breakpoint
CREATE TABLE "deadline" (
	"id" text PRIMARY KEY NOT NULL,
	"user_id" text NOT NULL,
	"course_id" text NOT NULL,
	"kind" text NOT NULL,
	"title" text NOT NULL,
	"due_at" date NOT NULL,
	"status" text NOT NULL,
	"origin" jsonb,
	"user_edited" jsonb DEFAULT '[]'::jsonb NOT NULL,
	"confidence" real DEFAULT 1 NOT NULL,
	"rough_minutes" integer,
	"rough_source" text,
	"time_adjust" real DEFAULT 1 NOT NULL,
	"target_grade" jsonb,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "digest_log" (
	"user_id" text NOT NULL,
	"date" date NOT NULL,
	"channel" text NOT NULL,
	"sent_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "digest_log_user_id_date_channel_pk" PRIMARY KEY("user_id","date","channel")
);
--> statement-breakpoint
CREATE TABLE "exam_result" (
	"id" text PRIMARY KEY NOT NULL,
	"user_id" text NOT NULL,
	"deadline_id" text NOT NULL,
	"course_id" text NOT NULL,
	"title" text NOT NULL,
	"kind" text NOT NULL,
	"due_at" date NOT NULL,
	"grade" jsonb,
	"score" real,
	"target" real NOT NULL,
	"minutes_in_app" integer NOT NULL,
	"minutes_outside" integer DEFAULT 0 NOT NULL,
	"base_estimate_min" integer NOT NULL,
	"felt_difficulty" integer,
	"topic_ratings" jsonb DEFAULT '{}'::jsonb NOT NULL,
	"excluded" boolean DEFAULT false NOT NULL,
	"note" text,
	"grade_due_at" date,
	"reminder_count" integer DEFAULT 0 NOT NULL,
	"recorded_at" timestamp with time zone DEFAULT now() NOT NULL,
	"graded_at" timestamp with time zone
);
--> statement-breakpoint
CREATE TABLE "google_connection" (
	"user_id" text PRIMARY KEY NOT NULL,
	"google_email" text,
	"refresh_token_enc" text NOT NULL,
	"access_token_enc" text,
	"expires_at" timestamp with time zone,
	"scope" text,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "inbox_entry" (
	"id" text PRIMARY KEY NOT NULL,
	"user_id" text NOT NULL,
	"change" text NOT NULL,
	"entity" text NOT NULL,
	"ref_id" text,
	"source_id" text NOT NULL,
	"external_id" text NOT NULL,
	"title" text NOT NULL,
	"payload" jsonb NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"resolved" boolean DEFAULT false NOT NULL
);
--> statement-breakpoint
CREATE TABLE "material" (
	"id" text PRIMARY KEY NOT NULL,
	"user_id" text NOT NULL,
	"course_id" text NOT NULL,
	"deadline_id" text,
	"title" text NOT NULL,
	"unit_type" text NOT NULL,
	"quantity" real NOT NULL,
	"difficulty" integer NOT NULL,
	"time_adjust" real DEFAULT 1 NOT NULL,
	"estimate_override" integer,
	"sort_order" integer DEFAULT 0 NOT NULL,
	"available_from" date,
	"done" boolean DEFAULT false NOT NULL,
	"origin" jsonb,
	"user_edited" jsonb DEFAULT '[]'::jsonb NOT NULL,
	"link" text,
	"note" text,
	"weak" boolean DEFAULT false NOT NULL
);
--> statement-breakpoint
CREATE TABLE "profile" (
	"user_id" text PRIMARY KEY NOT NULL,
	"settings" jsonb NOT NULL,
	"availability" jsonb NOT NULL,
	"plan_meta" jsonb NOT NULL,
	"next_digest_at" timestamp with time zone,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "push_subscription" (
	"id" text PRIMARY KEY NOT NULL,
	"user_id" text NOT NULL,
	"endpoint" text NOT NULL,
	"p256dh" text NOT NULL,
	"auth" text NOT NULL,
	"user_agent" text,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"last_success_at" timestamp with time zone,
	"failure_count" integer DEFAULT 0 NOT NULL
);
--> statement-breakpoint
CREATE TABLE "rate_limit" (
	"id" text PRIMARY KEY NOT NULL,
	"key" text NOT NULL,
	"count" integer NOT NULL,
	"last_request" bigint NOT NULL,
	CONSTRAINT "rate_limit_key_unique" UNIQUE("key")
);
--> statement-breakpoint
CREATE TABLE "session" (
	"id" text PRIMARY KEY NOT NULL,
	"expires_at" timestamp with time zone NOT NULL,
	"token" text NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL,
	"ip_address" text,
	"user_agent" text,
	"user_id" text NOT NULL,
	CONSTRAINT "session_token_unique" UNIQUE("token")
);
--> statement-breakpoint
CREATE TABLE "snapshot" (
	"id" text PRIMARY KEY NOT NULL,
	"user_id" text NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"label" text NOT NULL,
	"state" jsonb NOT NULL
);
--> statement-breakpoint
CREATE TABLE "source" (
	"id" text PRIMARY KEY NOT NULL,
	"user_id" text NOT NULL,
	"type" text NOT NULL,
	"name" text NOT NULL,
	"config" jsonb NOT NULL,
	"status" text NOT NULL,
	"interval_min" integer NOT NULL,
	"last_sync_at" timestamp with time zone,
	"last_change_at" timestamp with time zone,
	"etag" text,
	"last_modified" text,
	"error" text,
	"error_count" integer DEFAULT 0 NOT NULL,
	"needs_reconnect" boolean DEFAULT false NOT NULL,
	"next_sync_at" timestamp with time zone DEFAULT now() NOT NULL,
	"locked_until" timestamp with time zone
);
--> statement-breakpoint
CREATE TABLE "study_session" (
	"id" text NOT NULL,
	"user_id" text NOT NULL,
	"date" date NOT NULL,
	"material_id" text NOT NULL,
	"review_material_ids" jsonb,
	"deadline_id" text,
	"course_id" text NOT NULL,
	"kind" text NOT NULL,
	"minutes" integer NOT NULL,
	"pinned" boolean DEFAULT false NOT NULL,
	"status" text NOT NULL,
	"actual_minutes" integer,
	"reasons" jsonb DEFAULT '[]'::jsonb NOT NULL,
	CONSTRAINT "study_session_user_id_id_pk" PRIMARY KEY("user_id","id")
);
--> statement-breakpoint
CREATE TABLE "user" (
	"id" text PRIMARY KEY NOT NULL,
	"name" text NOT NULL,
	"email" text NOT NULL,
	"email_verified" boolean DEFAULT false NOT NULL,
	"image" text,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "user_email_unique" UNIQUE("email")
);
--> statement-breakpoint
CREATE TABLE "verification" (
	"id" text PRIMARY KEY NOT NULL,
	"identifier" text NOT NULL,
	"value" text NOT NULL,
	"expires_at" timestamp with time zone NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
ALTER TABLE "account" ADD CONSTRAINT "account_user_id_user_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."user"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "ai_usage" ADD CONSTRAINT "ai_usage_user_id_user_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."user"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "block" ADD CONSTRAINT "block_user_id_user_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."user"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "busy_event" ADD CONSTRAINT "busy_event_user_id_user_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."user"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "course" ADD CONSTRAINT "course_user_id_user_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."user"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "deadline" ADD CONSTRAINT "deadline_user_id_user_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."user"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "digest_log" ADD CONSTRAINT "digest_log_user_id_user_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."user"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "exam_result" ADD CONSTRAINT "exam_result_user_id_user_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."user"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "google_connection" ADD CONSTRAINT "google_connection_user_id_user_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."user"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "inbox_entry" ADD CONSTRAINT "inbox_entry_user_id_user_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."user"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "material" ADD CONSTRAINT "material_user_id_user_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."user"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "profile" ADD CONSTRAINT "profile_user_id_user_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."user"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "push_subscription" ADD CONSTRAINT "push_subscription_user_id_user_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."user"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "session" ADD CONSTRAINT "session_user_id_user_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."user"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "snapshot" ADD CONSTRAINT "snapshot_user_id_user_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."user"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "source" ADD CONSTRAINT "source_user_id_user_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."user"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "study_session" ADD CONSTRAINT "study_session_user_id_user_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."user"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
CREATE INDEX "account_user_idx" ON "account" USING btree ("user_id");--> statement-breakpoint
CREATE INDEX "ai_usage_user_idx" ON "ai_usage" USING btree ("user_id","created_at");--> statement-breakpoint
CREATE INDEX "ai_usage_cache_idx" ON "ai_usage" USING btree ("user_id","cache_key");--> statement-breakpoint
CREATE INDEX "block_user_idx" ON "block" USING btree ("user_id");--> statement-breakpoint
CREATE INDEX "busy_user_idx" ON "busy_event" USING btree ("user_id");--> statement-breakpoint
CREATE INDEX "course_user_idx" ON "course" USING btree ("user_id");--> statement-breakpoint
CREATE INDEX "deadline_user_idx" ON "deadline" USING btree ("user_id");--> statement-breakpoint
CREATE INDEX "result_user_idx" ON "exam_result" USING btree ("user_id");--> statement-breakpoint
CREATE UNIQUE INDEX "result_deadline_idx" ON "exam_result" USING btree ("user_id","deadline_id");--> statement-breakpoint
CREATE INDEX "inbox_user_idx" ON "inbox_entry" USING btree ("user_id");--> statement-breakpoint
CREATE INDEX "material_user_idx" ON "material" USING btree ("user_id");--> statement-breakpoint
CREATE INDEX "profile_next_digest_idx" ON "profile" USING btree ("next_digest_at");--> statement-breakpoint
CREATE INDEX "push_user_idx" ON "push_subscription" USING btree ("user_id");--> statement-breakpoint
CREATE UNIQUE INDEX "push_endpoint_idx" ON "push_subscription" USING btree ("endpoint");--> statement-breakpoint
CREATE INDEX "session_user_idx" ON "session" USING btree ("user_id");--> statement-breakpoint
CREATE INDEX "snapshot_user_idx" ON "snapshot" USING btree ("user_id");--> statement-breakpoint
CREATE INDEX "source_user_idx" ON "source" USING btree ("user_id");--> statement-breakpoint
CREATE INDEX "source_next_sync_idx" ON "source" USING btree ("next_sync_at");--> statement-breakpoint
CREATE INDEX "session_user_date_idx" ON "study_session" USING btree ("user_id","date");--> statement-breakpoint
CREATE INDEX "verification_identifier_idx" ON "verification" USING btree ("identifier");