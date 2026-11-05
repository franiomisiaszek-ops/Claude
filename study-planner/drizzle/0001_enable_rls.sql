-- Defense in depth for hosted Postgres (e.g. Supabase): enable row-level security with no policies,
-- so roles other than the table owner (such as Supabase's public "anon" Data API role) can't read anything.
-- The app connects as the owner, which bypasses RLS.
ALTER TABLE "user" ENABLE ROW LEVEL SECURITY;--> statement-breakpoint
ALTER TABLE "session" ENABLE ROW LEVEL SECURITY;--> statement-breakpoint
ALTER TABLE "account" ENABLE ROW LEVEL SECURITY;--> statement-breakpoint
ALTER TABLE "verification" ENABLE ROW LEVEL SECURITY;--> statement-breakpoint
ALTER TABLE "rate_limit" ENABLE ROW LEVEL SECURITY;--> statement-breakpoint
ALTER TABLE "profile" ENABLE ROW LEVEL SECURITY;--> statement-breakpoint
ALTER TABLE "course" ENABLE ROW LEVEL SECURITY;--> statement-breakpoint
ALTER TABLE "deadline" ENABLE ROW LEVEL SECURITY;--> statement-breakpoint
ALTER TABLE "material" ENABLE ROW LEVEL SECURITY;--> statement-breakpoint
ALTER TABLE "study_session" ENABLE ROW LEVEL SECURITY;--> statement-breakpoint
ALTER TABLE "block" ENABLE ROW LEVEL SECURITY;--> statement-breakpoint
ALTER TABLE "busy_event" ENABLE ROW LEVEL SECURITY;--> statement-breakpoint
ALTER TABLE "source" ENABLE ROW LEVEL SECURITY;--> statement-breakpoint
ALTER TABLE "inbox_entry" ENABLE ROW LEVEL SECURITY;--> statement-breakpoint
ALTER TABLE "snapshot" ENABLE ROW LEVEL SECURITY;--> statement-breakpoint
ALTER TABLE "digest_log" ENABLE ROW LEVEL SECURITY;--> statement-breakpoint
ALTER TABLE "google_connection" ENABLE ROW LEVEL SECURITY;--> statement-breakpoint
ALTER TABLE "push_subscription" ENABLE ROW LEVEL SECURITY;--> statement-breakpoint
ALTER TABLE "exam_result" ENABLE ROW LEVEL SECURITY;--> statement-breakpoint
ALTER TABLE "ai_usage" ENABLE ROW LEVEL SECURITY;
