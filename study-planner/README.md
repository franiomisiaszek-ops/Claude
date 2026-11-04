# Study Planner

Connect the apps you study with (Google Classroom, Google Calendar, Canvas, Moodle, Blackboard, Brightspace, Outlook), or type your exams in yourself. The app works out **what to study each day and for how long**, keeps syncing for new material and changed dates, re-plans automatically, and reminds you every morning, including on days off.

```bash
cd study-planner
npm install
npm start          # builds the UI and serves everything on http://localhost:3000
```

On first launch, a 3-step setup asks for your weekly study time and where your classes are. Choose **Try with demo data** to see a full plan right away.

Other commands: `npm test` (planner, classifier, sync and ICS tests), `npm run typecheck`, `npm run dev` (hot reload; UI at :5173 and API at :3000).

## What it does

| Feature | How |
|---|---|
| **Today screen** | Today's sessions with **Start (timer) / Done / Partly / Skip**. Shows "Day off" with the next session when nothing is planned. One-tap **Can't study today** moves everything forward. |
| **Auto study plan** | Each exam, quiz and assignment gets study sessions spread across the days before it. A buffer day before exams is kept for review only. Spaced reviews come +1/+3/+7 days after learning something. |
| **Why today?** | Every session explains itself, e.g. "Biology midterm is in 6 days · 90 of 335 min done · spaced review". |
| **Exam runway & plan health** | A readiness bar for each deadline (done / planned / doesn't fit). The plan never crams silently. If time runs short it says how much and suggests fixes, e.g. "Unblock 2 days (+204 min)" or "Add 15 min per study day". |
| **Edit anything** | In the agenda, change a session's length or date, pin it, remove it or add your own. In the week view, drag sessions between days. The month view is a load heatmap. Edited sessions are pinned, so re-planning leaves them alone. Undo is available after every change. |
| **Availability** | Minutes per weekday, an optional study window (busy calendar events inside it reduce study time), and blocked dates or ranges with a reason. |
| **Manual material** | Add exams and deadlines. Add material one item at a time or **paste a list**: "Chapter 3 – 24 pages", "Lecture 5: 40 slides" and "Revise notes 2h" are parsed automatically. Effort is estimated from pages, slides, problems or video minutes and difficulty, and you can override it. |
| **Learns your pace** | Logging how long a session took calibrates the estimates for that course after 3 samples. |
| **Continuous sync** | Background polling: Classroom every 30 min, Calendar every 15 min, ICS feeds every 3 h, plus a **Sync** button. Uses backoff on errors and shows "Reconnect" if Google access expires. |
| **Sync inbox** | Clear items go straight into the plan. Uncertain items, removals, date changes and conflicts with your own edits wait for review. If the teacher moves a date you changed, you choose which one to keep. |
| **Daily reminder** | A browser notification at your chosen time, an optional email digest through your own SMTP (sent after the morning sync), and a `.ics` export so your phone's calendar app alerts you. |

## Connecting apps

- **School LMS (no setup):** paste the calendar feed URL. The *Apps* page explains where to find it in Canvas, Moodle, Blackboard, Brightspace and Outlook. Course codes like `[BIO101]` become courses. Choose "Personal calendar" for a calendar feed, so ordinary events count as busy time.
- **Google Classroom + Calendar:** Google needs your own free OAuth client. The *Apps* page walks you through it: create a Cloud project, enable the Classroom and Calendar APIs, publish the consent screen to "In production" (in "Testing" mode tokens expire after 7 days), and create a *Web application* client with redirect URI `http://localhost:3000/oauth/google/callback`. Then paste the ID and secret and press **Connect Google**.
  - Every scope is read-only.
  - Imported: coursework (assignments, tests, quizzes), course materials linked to their topic's test, your submission state (turned in or returned counts as done), and calendar busy time.
  - Classroom's auto-created course calendars are skipped, so due dates aren't imported twice.
  - Some school Workspace admins block unverified apps. In that case use the LMS feed or Google Calendar's secret iCal address instead.
- **Demo data:** a sample term with four courses. Remove it any time from *Apps*.

## How the planner works

`src/planner/planner.ts` is a pure, deterministic function `plan(state, today)`. It does no I/O and never reads the clock, which makes it easy to unit-test.

1. **Reconcile the past.** Unmarked past sessions become *missed*, and their time returns to the pool. Done and partial minutes count as progress.
2. **Keep fixed sessions.** Pinned sessions, anything already checked in, and today's sessions once they have been planned stay where they are. Today's plan doesn't shuffle under you.
3. **Estimate** remaining minutes per material. Base rate × amount × difficulty × your calibration, or your override. An exam with no material listed reserves a default block (exam 3 h, quiz 1 h, assignment 1.5 h).
4. **Capacity** per day = weekly minutes, capped by your study window minus busy events in it, × 85% slack. Blocked days have zero capacity.
5. **Windows.** New material must be finished before the buffer: 2 days before exams, 1 day before quizzes and assignments.
6. **Earliest deadline first, water-filled.** Each deadline's work is spread evenly across its window in 25–60 min chunks, in syllabus order, with at most 3 subjects per day. Existing sessions stay where they still fit, so a sync doesn't reshuffle the calendar.
7. **Triage.** If a deadline doesn't fit, its buffer shrinks. If it still doesn't fit, the plan reports the shortfall and suggests fixes. Reviews only ever use leftover time.
8. **Reviews.** For exams and quizzes: spaced reviews, plus a consolidation session on each buffer day.

## Project layout

```
src/planner/   planner + effort estimator (pure, unit-tested)
src/server/    Express API, JSON store (data/db.json), sync/merge, classifier,
               connectors/ (demo, ics, google), daily digest, .ics export
src/client/    React UI (Today, Plan, Courses, Inbox, Apps, Availability, Settings)
src/shared/    types and date/label helpers used by both sides
```

Data stays on your machine in `./data/` (`DATA_DIR` overrides it). OAuth tokens are kept in a separate file with owner-only permissions. Set `PORT` and `PUBLIC_URL` if you host it elsewhere, and update the Google redirect URI to match.

## How this was designed

Three agents brainstormed ideas in parallel: the scheduling engine, integrations and sync, and UX. A fourth agent judged all 36 ideas for student value, feasibility and risk, and merged overlapping ones. It accepted 13 as-is and 15 in simplified form, deferred 5 and rejected 1. It also flagged 8 claims as wrong or conflicting with the constraints. For example, subtracting *all* busy time from a day's budget is wrong (a 9 am lecture doesn't reduce evening study time), and pg-boss would have needed Postgres. This app implements the judged V1 spec.

**Deferred:** web push and PWA install (both need HTTPS and a server your phone can reach), writing the plan back into Google Calendar, Canvas/Moodle/Notion API tokens, syllabus PDF parsing, a what-if simulator, streaks.

## Limitations

- Background sync and the email digest only run while the app is running. Browser notifications fire only while a tab is open.
- The ICS export is a download, not a live subscription, because calendar services can't reach `localhost`.
