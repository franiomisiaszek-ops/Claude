# Running papertrader on your Windows PC

This guide covers everything after the first setup: keeping the PC ready, backups,
updates, and what to do when something goes wrong. Every command is typed in
**PowerShell**, in the `trading-agent` folder. To open one: in File Explorer, open the
`trading-agent` folder, click the address bar, type `powershell`, press Enter.

- [1. How it fits together](#1-how-it-fits-together)
- [2. First setup (once)](#2-first-setup-once)
- [3. Keep the PC ready](#3-keep-the-pc-ready)
- [4. Everyday use](#4-everyday-use)
- [5. Backups](#5-backups)
- [6. Restoring a backup](#6-restoring-a-backup)
- [7. Updating to a new version](#7-updating-to-a-new-version)
- [8. When something goes wrong](#8-when-something-goes-wrong)
- [9. Safety notes](#9-safety-notes)

## 1. How it fits together

Docker Desktop runs four small Linux "containers" on your PC:

| Container | What it does |
|---|---|
| `db` | The PostgreSQL database: prices, news, decisions, trades, everything |
| `app` | The scheduler: the daily jobs, the Telegram bot, PAPER trading on Alpaca |
| `web` | The web app at <http://localhost:8501> (login required; on the iPhone through Tailscale, see [WEBAPP.md](WEBAPP.md)) |
| `backup` | One backup of the database and the models every night |

They restart by themselves if they crash and when Docker Desktop starts. Your data lives
in Docker "volumes" that survive restarts, rebuilds and updates. Only
`docker compose down -v` deletes them: never type `-v` unless you want to start over.

The jobs run in New York time. Poland is usually 6 hours ahead (5 hours for a week or
two in March and in late October/November, when the clocks change on different dates):

| New York | Poland (usual) | What |
|---|---|---|
| 06:30 | 12:30 | Load data |
| 07:30 - 08:45 | 13:30 - 14:45 | Research, judge, morning brief |
| 09:35 - 16:00 | 15:35 - 22:00 | Orders, then the monitor every 10 minutes |
| 16:15 - 17:00 | 22:15 - 23:00 | End-of-day summary, scoring past calls |
| about 00:00 | about 06:00 | Backup |
| Saturday 09:00 | Saturday 15:00 | Weekly learning, retrain, summary |

## 2. First setup (once)

You have done most of this already. For a new PC, or to check:

1. **Windows 10 version 22H2 or Windows 11**, 64-bit, with at least 8 GB of memory and
   20 GB of free disk space.
2. **Virtualization on.** Task Manager > Performance > CPU should say
   "Virtualization: Enabled". If not, it has to be switched on in the BIOS (search the
   web for your PC model + "enable virtualization").
3. **WSL 2.** Open PowerShell *as administrator* (right-click the Start button >
   "Windows PowerShell (Admin)") and run the command below, then restart the PC:

   ```bash
   wsl --install
   ```

4. **Docker Desktop**: install it from docker.com, then open it. In Settings > General,
   tick **"Use the WSL 2 based engine"** and **"Start Docker Desktop when you sign in"**.
5. **Git for Windows** from git-scm.com, then get the code:

   ```bash
   git clone https://github.com/franiomisiaszek-ops/Claude.git
   ```

   ```bash
   cd Claude\trading-agent
   ```

   ```bash
   git checkout claude/intelligent-lamport-g6gwc7
   ```

6. **Your keys**: `copy .env.example .env`, then fill in `.env` with Notepad (README,
   section "Your keys"). Change `POSTGRES_PASSWORD` *before* the first start.
7. **Start**. First build system A's frozen image ([EXPERIMENT.md](EXPERIMENT.md),
   step 4), then:

   ```bash
   docker compose up -d --build
   ```

8. **Check**: `docker compose exec app papertrader check` should end with `OK`, and
   show `Clock : OK`.
9. **Load history and train** (README, Phases 2 and 4), then set up Telegram
   (README, section Telegram).

## 3. Keep the PC ready

The system only works while the PC is **on, awake and signed in**, with Docker
Desktop running. Stops and targets of open positions sit at Alpaca and work regardless,
but research, orders, time stops and messages need the PC.

**Never sleep (when plugged in).** Settings > System > Power & sleep > Sleep: **Never**.
The screen may turn off; that's fine. On a laptop: Control Panel > Power Options >
"Choose what closing the lid does" > **Do nothing** (when plugged in).

**Windows Update restarts.** Settings > Update & Security > Windows Update > "Change
active hours": set **7:00 to 1:00** (the maximum is 18 hours). Windows then only
restarts between 1:00 and 7:00, outside all trading jobs. A missed backup is retried
until it succeeds that day.

**After a restart**, Docker Desktop only starts once you sign in. If you're not there to
sign in, nothing runs until you are. When the app starts again, Telegram tells you how
long it was off and which jobs were missed. Optional, if the PC is somewhere private:
automatic sign-in (press Win+R, type `netplwiz`, untick "Users must enter a user name
and password"; with a Microsoft account first turn off Settings > Accounts > Sign-in
options > "Require Windows Hello sign-in"). This means anyone at the PC is signed in as
you, so only do it if that's acceptable.

**Don't quit Docker Desktop.** Closing its window is fine (it keeps running in the
system tray, next to the clock). "Quit Docker Desktop" stops everything.

**Clock.** The app checks every hour that the PC's clock matches the market's and
alerts you if it is off by more than a minute. Docker's clock can drift after the PC
sleeps; restarting Docker Desktop fixes it. Windows: Settings > Time & Language >
Date & time > "Sync now".

**Disk space.** Keep at least 10 GB free. Logs are capped (50 MB per container),
backups keep the newest 7, and old training datasets are deleted automatically.

## 4. Everyday use

You don't need to do anything: Telegram tells you what happens. To look closer:

- **Web app**: <http://localhost:8501> on this PC, and on your iPhone through Tailscale
  ([WEBAPP.md](WEBAPP.md)). A, B and C side by side, the comparison, trades, planned
  trades, decisions and agent reports (with sources), news, post-mortems, costs, jobs.
  Read-only apart from Pause / Resume / Skip, which ask you to confirm.
- **Bot**: `/status`, `/positions`, `/pnl`, `/brief`, `/pause`, `/resume`, `/skip`, `/why`,
  `/ask <question>`. Orders of B and C come with a heads-up and Skip / Go now buttons
  3 minutes before; no answer means they go ahead.
- **Today's jobs**:

  ```bash
  docker compose run --rm app papertrader jobs list
  ```

- **Recent runs**:

  ```bash
  docker compose run --rm app papertrader jobs history
  ```

- **Run a missed job by hand** (e.g. the PC was off in the morning):

  ```bash
  docker compose run --rm app papertrader jobs run research_agents
  ```

  The morning order is `ingest`, `research_agents`, `judge`, `morning_brief`,
  `place_orders`. Orders are only placed while the market is open.

- **Are all four containers running?** `docker compose ps` should show `running` and
  `healthy` for each.

## 5. Backups

**Automatic.** Every night (about 06:00 Polish time) the `backup` container writes two
files into the `backups` folder next to `docker-compose.yml`:

- `papertrader-<date>_<time>.dump`: the whole database (decisions, trades, outcomes,
  prices, news, everything).
- `models-<date>_<time>.tgz`: the trained pattern models.

The newest 7 are kept. If backups stop, the app tells you on Telegram within a day and
a half. To see what the backup container did: `docker compose logs backup`.

**A backup right now** (e.g. before an update):

```bash
docker compose exec backup sh /scripts/backup.sh now
```

**A copy somewhere else.** A backup on the same disk doesn't help if the disk dies. The
simplest fix is to let the backups land in your OneDrive folder, which copies them to
the cloud. Add this line to `.env` (with your own Windows user name; use forward
slashes), then run `docker compose up -d`:

```
BACKUP_DIR=C:/Users/your-name/OneDrive/papertrader-backups
```

Or copy the `backups` folder to a USB stick now and then. To keep more than 7, add
`BACKUP_KEEP=14` to `.env`.

**Your keys are not in the backups.** Keep a copy of your `.env` file in a password
manager or another safe place; without it you'd have to create new keys.

## 6. Restoring a backup

This replaces the whole database with the backup. Anything that happened after the
backup is lost (the paper account at Alpaca is not affected; the app catches up with it
on its next check).

1. Stop the apps and the web app:

   ```bash
   docker compose stop app app-b app-c hub web
   ```

2. See which backups exist:

   ```bash
   docker compose exec backup sh /scripts/restore.sh
   ```

3. Restore one, typing its name exactly as listed. Answer `YES` when asked:

   ```bash
   docker compose exec backup sh /scripts/restore.sh /backups/papertrader-2026-11-19_0500.dump
   ```

   It works in one go: if anything goes wrong, the database stays as it was.

4. Start again:

   ```bash
   docker compose start app app-b app-c hub web
   ```

**On a new PC:** do the first setup (section 2) with the same `.env`, start once,
copy your backup files into the new `backups` folder, then follow steps 1 to 4.

## 7. Updating to a new version

Update outside the trading hours: after 23:30 or before 12:00 Polish time, or at the
weekend.

1. Make a backup:

   ```bash
   docker compose exec backup sh /scripts/backup.sh now
   ```

2. Get the new version:

   ```bash
   git pull
   ```

3. Rebuild and restart. Database changes, if any, are applied automatically when the
   app starts. (During the three-way experiment this rebuilds B, C, the hub and the
   web app only; system A keeps running its frozen image. See
   [EXPERIMENT.md](EXPERIMENT.md).)

   ```bash
   docker compose up -d --build
   ```

4. Check:

   ```bash
   docker compose exec app papertrader check
   ```

   ```bash
   docker compose run --rm app papertrader jobs list
   ```

**If the new version misbehaves**, go back to the previous one:

```bash
git log --oneline -5
```

This lists the last versions, newest first. Copy the code at the start of the second
line (e.g. `a4b1cfe`), then:

```bash
git checkout a4b1cfe
```

```bash
docker compose up -d --build
```

During the experiment, do not go back to a version from before 20 Nov 2026 this way:
the old `docker-compose.yml` knows nothing about B, C and the hub.
If `docker compose logs app` showed "migrating" during the update, also restore the
backup from step 1 (section 6). Later, to return to the newest version:
`git checkout claude/intelligent-lamport-g6gwc7`, then `git pull` and step 3.

## 8. When something goes wrong

| What you see | What to do |
|---|---|
| No Telegram messages at all | `docker compose ps`: is `app` running? `docker compose logs app --tail 100` for errors. `docker compose run --rm app papertrader telegram send` shows Telegram problems. |
| "These jobs did not run today" | The PC was off, asleep or not signed in. Run the missed jobs by hand (section 4) if it's still early enough. |
| "The app was not running from ... to ..." | Information only: it was off (restart, sleep). Check the PC's power settings (section 3). |
| A job failed | `docker compose run --rm app papertrader jobs history` shows the reason. Most fix themselves the next day; tell me the message if one keeps failing. |
| "clock is ... seconds ahead/behind" | Restart Docker Desktop (tray icon > Restart), then Windows Settings > Time > Sync now. |
| "The newest database backup is ... old" | `docker compose logs backup` shows why. `docker compose up -d` restarts it. |
| Web app doesn't open | `docker compose ps`; `docker compose restart web`. On the PC: <http://localhost:8501>. On the iPhone: Tailscale must be on ([WEBAPP.md](WEBAPP.md)). |
| "Docker Desktop is starting" forever | Restart the PC. If it persists: in admin PowerShell, `wsl --update`, then restart. |
| Disk full | `docker system df` shows Docker's use. `docker image prune` removes old app images safely. Never `docker volume prune`: that deletes your data. |
| `\r: not found` in the backup logs | The scripts got Windows line endings. Run `Remove-Item scripts\*.sh`, then `git checkout -- scripts`, then `docker compose up -d`. |

To see everything the app logged recently: `docker compose logs app --since 2h`.

## 9. Safety notes

- **Paper trading only.** The code refuses anything but Alpaca's paper endpoint and
  refuses live keys. There is no setting to change this.
- **Keys stay in `.env`.** It is never committed to git and never in backups.
- **The web app listens on this PC only** (127.0.0.1); your iPhone reaches it through
  Tailscale. Don't forward port 8501 on your router, and never use `tailscale funnel`.
- **The database isn't reachable from outside Docker at all.**
- **Windows 10** stopped getting free security updates in October 2025, and Docker
  Desktop may stop supporting it in a future release. The system keeps working on the
  installed version; keep Windows Defender on, and plan a move to Windows 11 or a small
  Linux server for a long run.
