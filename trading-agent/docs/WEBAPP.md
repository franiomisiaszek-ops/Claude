# The iPhone web app (phase X5)

A private web app for A, B and C, made for the iPhone first ("Add to Home Screen"), and
fine on the PC too. It replaces the old Streamlit dashboard.

## What it shows

| Tab | What is on it |
|---|---|
| **Today** | A, B and C side by side: account value, today's change, today vs SPY, since the start vs SPY, open positions, Claude spend, alerts, whether each system is running or paused |
| **Compare** | The experiment: return after costs, return vs SPY, drawdown (charts), and the numbers: Sharpe, hit rate, IC, Brier, trades, dollars traded, estimated trading costs, Claude and data costs, net profit at $1,000, the pre-registered tests |
| **Trades** | Open positions with live P&L, entry, target, stop and time left; closed trades with how they ended and which agents were right; C's orders |
| **Planned** | What is about to be bought or sold, why, and when: A's and B's ideas before 09:35, sells waiting, B's intraday plans (with **Skip**), C's weekly plan |
| **More** | C portfolio (weekly plan, the Avanza list, its backtest) · Decisions and reports (every agent report with its sources) · News on held stocks · Post-mortems · Agents · Pattern model · Costs · Jobs and messages · Settings and buttons |

Live prices come from the hub, which stores each paper account's numbers every 2 minutes
while the market is open. The page says how old they are. Today, Trades and Planned
reload themselves while they are on screen; the ↻ button reloads any page.

**Read-only by default.** Only three buttons change something: **Pause** or **Resume** a
system, and **Skip** one of B's planned intraday trades. Each one opens a "Please confirm"
page first, is written to the activity log (Settings), and sends you a Telegram message.
Skip buttons for every order come with the Telegram upgrade (phase X6).

## Hosting: what was chosen

| Option | Cost a month | |
|---|---|---|
| **(a) On your PC, reached through Tailscale** | **$0** | **Chosen.** Nothing is open to the internet. Works whenever the PC is on. |
| (b) A small server for the web app only, fed a read-only copy of the data | about $5-7 | Works when the PC is off, but only shows data up to the last copy |
| (c) Everything on a server, 24/7, without your PC | roughly $8-50 (4 vCPU and 8 GB RAM for the weekly model retraining; cheapest at European hosts) | Only worth it if the PC can't stay on |

## Security

* **Only your devices can reach it.** It listens on the PC only (`127.0.0.1:8501`, never
  on your home network). Tailscale makes a private network of your own devices and
  carries the app to your iPhone. No router port is opened.
* **HTTPS**, with a real certificate from Tailscale for your PC's private name
  (`your-pc.your-tailnet.ts.net`).
* **A login** with a long password (16+ characters). Only a slow, salted hash (scrypt) is
  stored, in the app's data folder. After 5 wrong passwords in a row, logins lock for 1
  minute, then 2, 4 and so on up to an hour, and you get a Telegram alert.
* **Nothing is visible without logging in** except the login page, the icons and an
  "offline" page. The session cookie is HttpOnly, Secure and signed; forms carry a CSRF
  token; strict security headers forbid framing and outside scripts.
* **No keys in the app.** The web container gets only the database address. Live prices
  come from the hub, which already holds the paper keys. The browser never sees any key.
* **The database is never exposed.** It has no port outside Docker, and the pages read it
  through connections that refuse writes.

## Set it up (once)

### 1. Update and start

In PowerShell, in the `trading-agent` folder:

```bash
git pull
```

```bash
docker compose up -d --build --remove-orphans
```

`--remove-orphans` removes the old dashboard container.

### 2. Choose the password

```bash
docker compose exec web papertrader web set-password
```

Type it twice (nothing shows while you type). Four or five unrelated words with spaces
work well. Save it in your iPhone's **Passwords** app.

Check on the PC: open <http://localhost:8501> and log in.

### 3. Tailscale on the PC (free)

1. Go to <https://tailscale.com>, **Get started**, and sign in (Google, Microsoft or Apple).
2. Download and install **Tailscale for Windows**. Sign in with the same account. A
   Tailscale icon appears next to the clock.
3. In the Tailscale admin page (<https://login.tailscale.com/admin/dns>), turn on
   **MagicDNS** and **HTTPS Certificates**.
4. In PowerShell:

   ```bash
   tailscale serve --bg 8501
   ```

   It prints your app's address, like `https://your-pc.tail1234.ts.net`. The `--bg` makes
   it stay on after restarts. (Only your Tailscale devices can open it; this is not
   `tailscale funnel`, which would put it on the internet. Never use funnel for this.)

### 4. On the iPhone

1. Install **Tailscale** from the App Store, sign in with the **same** account, and switch
   it on.
2. In **Safari**, open the address from step 3 (`https://your-pc....ts.net`).
3. Tap **Share** (the square with an arrow), then **Add to Home Screen**, then **Add**.
4. Open **papertrader** from the home screen and log in once (the iPhone can fill the
   saved password with Face ID). It stays logged in for 30 days.

### If it doesn't open

| Problem | Fix |
|---|---|
| "Can't reach your PC" | Tailscale on the phone is off, or the PC / Docker Desktop is not running |
| The address doesn't load at all | In PowerShell: `tailscale serve status`. If empty, run `tailscale serve --bg 8501` again |
| "No password is set yet" | Step 2 |
| "Too many wrong passwords" | Wait the minutes shown. If it wasn't you: check the devices in the Tailscale admin page, and run `docker compose exec web papertrader web logout-all` |
| Lost phone | Remove it in the Tailscale admin page (Machines), then `docker compose exec web papertrader web logout-all` |

## For developers

* Code: `src/papertrader/web/` (`app.py` routes and security, `auth.py` password,
  sessions, CSRF, lockout, `data.py` page queries, `live.py` the hub's snapshot,
  `actions.py` the confirmed buttons and the activity log, `templates/`, `static/`).
* Run without Docker: `uv run papertrader web serve` (then <http://localhost:8501>).
* Settings: `web:` in `config.yaml` (session length, minimum password length, allowed host
  names, lockout).
* Icons: `uv run --with pillow python scripts/make_icons.py`.
