# Cramless

Study plans built from your real material. Connect your classes or type them in, and Cramless works out **what to study each day and for how long**, keeps the plan up to date, reminds you every morning, and **learns from your grades**.

- **Put it online for free:** see [DEPLOY.md](DEPLOY.md). It's click-by-click, takes about 30 minutes, and needs no coding.
- **Run it on your computer:** `npm install && npm start`, then open http://localhost:3000. It uses a built-in database, so no setup is needed.

## What it does

**Plans from material, never invented time**
- Every minute comes from the material: rate per unit × amount (pages, slides, problems, video minutes, topics) × how hard it is *for you* × your pace in that course.
- An exam with no material gets **no time**. Instead it shows *Not planned yet*. You can fix that by:
  - adding material by hand
  - pasting a list
  - uploading a PDF, slides or a Word file
  - applying the scope from the Classroom description ("Test on chapters 4–6")
  - entering a time yourself, or reusing "same as last time" from your own history
- Each item has **Easy / Normal / Hard for me** and a **less ← → more time** control. Each exam has a more/less control for that exam only.
- Tap any estimate to see the chain behind it: "22 pages × 4 min → hard ×1.4 → more time ×1.25 = 2h 35m".

**Learns from grades**
- After an exam there's a quick debrief: time logged, time studied outside the app, how the exam felt, and which topics were weak.
- You enter the grade now or later, in your own scale. Supported scales: %, points, A–F, GPA, UK classes, IB, 1–10, 0–20, Polish 1–6 and 2–5, German 1–6, Czech 1–5, pass/fail.
- The model is an exponential learning curve fitted per course. It's explained under *How the learning works* below.

**The plan**
- Earliest deadline first, spread evenly across the days before it.
- Buffer days before exams, spaced reviews, missed-session rollover.
- Sessions you place yourself are pinned. Plan health warns when something doesn't fit and suggests concrete fixes.
- Agenda, week (drag to move) and month heatmap views. Every change can be undone.

**Connected apps**
- Google Classroom and Google Calendar (read-only).
- Calendar feeds from Canvas, Moodle, Blackboard, Brightspace and Outlook.
- Background sync with an inbox for anything uncertain, removed, or in conflict with your own edits.

**AI (optional, Claude)**
- Uploaded documents: the file is counted locally (exact pages and slides), and Claude proposes chapter boundaries and density from a compact digest. Code checks every page range and quote.
- Syllabus import: Claude finds the exams and deadlines; code resolves every date ("Week 6 Friday" → a date); the results go to your inbox for review.
- Classroom descriptions: their scope is read.
- Nothing is added without your approval.
- Guardrails: per-user daily quota, monthly budget cap, result caching, contact details removed, documents treated as untrusted data.

**Reminders**
- An installable app (PWA) with push notifications every morning: today's study, or "day off".
- Optional email. A `.ics` export for any calendar app.

**Accounts and privacy**
- Email/password or Google sign-in. Data download and full account deletion.
- Encrypted Google tokens. Row-level security on every table.
- Privacy policy and terms, including Google's Limited Use statement.

## How the learning works

For each graded exam, `x` = minutes studied ÷ the material estimate and `S` = grade as a share of points. The model is `S = 1 − e^(−x/τ)`, fitted per course on `θ = ln τ`:

- **Weights:**
  - recency: 0.7 per later exam
  - information: grades near 0% or 100% say little about effort
  - reliability: lower when much of the study happened outside the app
  - felt difficulty
  - outliers are damped
- **Shrinkage:** each course is pulled toward your other courses, and those toward a prior that reproduces the material-only plan.
- **Time factor:** for your target `T`, `g = τ·(−ln(1−T))`.

Guardrails:
- Nothing changes until your first graded exam.
- At most +30% per exam. A decrease needs two exams in a row and is at most −10% per exam.
- `g` stays within 0.7–3.
- `g < 1` only trims extra review, never the time needed to cover the material.
- You can turn learning off per course, or exclude any exam ("I was sick").

The same reasoning also fixed a calibration bug: the old pace average converged to the square root of your real pace.

## Tech

- **Code:** TypeScript throughout. React + Vite frontend; Express 5 API; deterministic, pure planner and learning modules.
- **Database:** Postgres through Drizzle. Supabase in production, embedded PGlite locally and in tests.
- **Accounts:** Better Auth.
- **Background jobs:** sync and digests. Work is claimed with `FOR UPDATE SKIP LOCKED` leases, safe across instances and sleeping hosts. They're driven in-process and by an external pinger calling `/api/cron/tick`.
- **Security:**
  - per-user advisory locks; CSP; an origin check on writes; rate limits
  - SSRF-safe feed fetching, checked at connect time
  - AES-256-GCM token encryption

```
src/planner/   planner, effort estimator, learning model (pure, unit-tested)
src/server/    API (app.ts), auth, db (schema + migrations in /drizzle), sync & connectors,
               jobs, digest & push, AI (extract, material, syllabus)
src/client/    React app (Today, Plan, Courses, Progress, Inbox, Apps, Availability, Settings)
src/shared/    types, grade scales, scope parser, date and label helpers
```

`npm test` runs the planner, learning, grade, classifier, sync, ICS and API integration tests. `npm run typecheck` type-checks. `npm run dev` runs with hot reload.

## How it was designed

The app was designed in two rounds. Each time, agents brainstormed in parallel and a judge agent scored every idea for value, validity, cost and risk, flagging wrong claims; the accepted ideas were built.

- **Round 1:** the planner, integrations and UX.
- **Round 2:** grade learning and time controls, Claude features and cost, hosted architecture and privacy.

Rejected as AI slop: AI-invented study minutes or dates, generic "coach" messages, and chat summaries.
