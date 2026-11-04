ALTER TABLE "exam_result" ADD COLUMN "base_source" text DEFAULT 'material' NOT NULL;--> statement-breakpoint
ALTER TABLE "study_session" ADD COLUMN "pace" real;