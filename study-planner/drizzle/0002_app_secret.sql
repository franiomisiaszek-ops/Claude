CREATE TABLE "app_secret" (
	"key" text PRIMARY KEY NOT NULL,
	"value_enc" text NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
ALTER TABLE "app_secret" ENABLE ROW LEVEL SECURITY;
