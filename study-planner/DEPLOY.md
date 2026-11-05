# Putting Cramless online (free)

This guide takes about 30 minutes, needs no coding, and costs nothing. You'll use three free services:

| Service | What it does | Free limit |
|---|---|---|
| **Supabase** | The database (accounts and study plans) | 500 MB. Pauses after ~1 week with no visitors; restore it in one click |
| **Render** | Runs the app and gives it a web address | One always-on app. Sleeps after 15 minutes without visits |
| **cron-job.org** | Wakes the app every 5 minutes so syncing and morning reminders run | Unlimited |

Optional add-ons:
- **Claude API key:** AI reads your PDFs and syllabi. Paid by usage, from your $5 credit.
- **Google sign-in + Classroom:** free.
- **A domain name** (~$10/year). Needed for email and for Google's app verification.

> Keep a password manager or a private note open. You'll copy several values between websites. Never paste keys into chats.

---

## Step 1: Create the database (Supabase)

1. Go to **supabase.com** → **Start your project** → sign in with GitHub.
2. Click **New project**.
   - Name: `cramless`
   - Database password: click **Generate a password** and **copy it to your note**.
   - Region: the one closest to you (for example *Central EU (Frankfurt)*).
   - Click **Create new project** and wait about 2 minutes.
3. At the top of the project page, click **Connect**.
4. Choose **Session pooler**. This matters: the other options don't work with Render.
5. Copy the connection string. It looks like
   `postgresql://postgres.abcdefgh:[YOUR-PASSWORD]@aws-0-eu-central-1.pooler.supabase.com:5432/postgres`
6. In your note, replace `[YOUR-PASSWORD]` with the password from step 2 (no square brackets). If the password contains symbols like `@ # / ?`, generate a new one without symbols instead.
7. Recommended: **Project Settings → Data API → turn off "Enable Data API"**. The app doesn't use it. Its tables are also locked with row-level security, so this is a second layer.

## Step 2: Put the app online (Render)

1. Go to **render.com** → **Get Started** → sign up with **GitHub**. Allow it to see the repository **franiomisiaszek-ops/claude**.
2. Click **New +** → **Blueprint**.
3. Pick the repository **franiomisiaszek-ops/claude** and the branch **claude/study-planner-app**. Render finds the file `render.yaml`.
4. Render asks for the values marked as needed:
   - **DATABASE_URL:** paste the Supabase connection string from Step 1.
   - **PUBLIC_URL:** for now type `https://cramless.onrender.com`. You'll correct it in step 6 if your address is different.
   - Leave everything else empty for now (AI, Google, email). Each is an optional feature.
5. Click **Apply**. The first build takes 3–5 minutes.
6. When it says **Live**, open the service. At the top you'll see its address, like `https://cramless-xyz1.onrender.com`. If it differs from what you typed:
   - **Environment** → edit **PUBLIC_URL** → paste the exact address (no `/` at the end) → **Save, rebuild and deploy**.
7. Open the address. You should see the sign-in page. Create your account.
8. Back in **Environment**, set **ADMIN_EMAILS** to the email you signed up with. After you confirm that email, Settings shows a **Site status** card with:
   - users and background jobs
   - AI spend
   - a proxy check: the address shown there should be your real internet address. If it isn't, set `TRUST_PROXY=2`.

Render generated three secrets for you (`BETTER_AUTH_SECRET`, `TOKEN_ENCRYPTION_KEY`, `CRON_SECRET`). **Don't change them later**: changing them signs everyone out and disconnects Google.

## Step 3: Keep it awake (cron-job.org)

A free Render app sleeps when nobody visits. This step wakes it every 5 minutes so it can sync apps and send the morning reminders.

1. In Render → your service → **Environment**, find **CRON_SECRET** → click the eye icon → copy the value.
2. Go to **cron-job.org** → **Sign up** (free) → **Create cronjob**.
   - Title: `Cramless tick`
   - URL: `https://YOUR-ADDRESS/api/cron/tick`
   - Schedule: **every 5 minutes**
   - **Advanced** tab:
     - Request method: **POST**
     - Headers → add `Authorization` with the value `Bearer ` followed by the secret (for example `Bearer 3f9c…`). There's a space after "Bearer".
   - Turn on **Notify me on failure** → **Create**.
3. Click **Test run**. It should show **200 OK**.
4. Recommended: **betterstack.com** (free) → **Uptime** → new monitor for `https://YOUR-ADDRESS/api/health`. It emails you if the site goes down.

## Step 4 (optional): AI document and syllabus reading (Claude)

Your $5 Console credit pays for this. Paying alone doesn't create a key; you create one:

1. Go to **console.anthropic.com** → **Settings → API keys** → **Create key**. Name it `cramless` and copy it. It starts with `sk-ant-`. You only see it once.
2. **Settings → Limits:** set a monthly spend limit (for example $5) so you can never be charged more.
3. Render → your service → **Environment** → **ANTHROPIC_API_KEY** → paste → **Save, rebuild and deploy**.

Built-in limits protect your credit:
- each user gets **10 analyses per day** (`AI_DAILY_LIMIT`)
- AI pauses for everyone once the month's spend reaches **$4** (`AI_MONTHLY_BUDGET_USD`)
- the same file is never analyzed twice

**Model and cost.** The app uses Claude Opus 5.5 by default, about $0.07 for a typical PDF analysis, so $5 is roughly 70 analyses. Setting the variable `ANTHROPIC_MODEL` to `claude-haiku-5-5` makes it about 40× cheaper, but it is less capable. That choice is yours.

**Minors.**
- Everyone is asked their birth month and year once, during setup. Accounts of people under 13 are deleted immediately.
- AI features are switched off for anyone under 18, as Anthropic's guidelines for organizations serving minors recommend. Under-18s still get everything else, including exact page counts from uploaded files.
- Only set `AI_ALLOW_MINORS=true` after you've added the safeguards those guidelines describe.

**Per-user limit.** `AI_USER_MONTHLY_USD`, $1 by default, stops one person from using up the whole month's budget.

## Step 5 (optional): Google sign-in, Classroom and Calendar

1. Go to **console.cloud.google.com** → create a project named `Cramless`.
2. **APIs & Services → Library:** enable **Google Classroom API** and **Google Calendar API**.
3. **Google Auth Platform → Branding:**
   - Fill in the app name, a support email, and the homepage `https://YOUR-ADDRESS`.
   - Privacy policy: `https://YOUR-ADDRESS/privacy`. Terms: `https://YOUR-ADDRESS/terms`.
   - **Don't upload a logo yet.** A logo triggers a slower review.
4. **Audience:** **External**. Add yourself as a test user.
5. **Clients → Create client → Web application.** Under **Authorized redirect URIs**, add both:
   - `https://YOUR-ADDRESS/api/auth/callback/google` (sign in with Google)
   - `https://YOUR-ADDRESS/oauth/google/callback` (connect Classroom/Calendar)
6. Copy the **Client ID** and **Client secret** into Render's **GOOGLE_CLIENT_ID** and **GOOGLE_CLIENT_SECRET** → **Save, rebuild and deploy**.
7. **Data Access → Add scopes:** `classroom.courses.readonly`, `classroom.coursework.me.readonly`, `classroom.courseworkmaterials.readonly`, `classroom.topics.readonly`, `calendar.calendarlist.readonly`, `calendar.events.readonly`.
8. **Audience → Publish app.** If you leave it in "Testing", Google signs people out every 7 days.

Until Google verifies the app:
- People see an "unverified app" warning and click *Advanced → continue*.
- At most 100 people can connect Classroom/Calendar.

To remove both limits, request verification in **Verification Center**. Google requires:
- your own domain (Step 7), verified in Google Search Console
- the privacy policy on that domain
- a short YouTube video showing how the app uses each permission
- about a week

**This is the main thing to start early.** Google verification is what lets the app grow past 100 users with Classroom. It takes days to weeks, so start it as soon as you have a domain.

**School accounts:**
- Google blocks students under 18 from apps their school hasn't approved.
- The page `https://YOUR-ADDRESS/schools` explains to school IT exactly how to approve the app, and includes an email students can forward.
- Until it's approved, those students can use their school's calendar feed instead.

## Step 6 (optional): Email (confirm accounts, reset passwords, morning email)

Needs your own domain (Step 7).

1. **resend.com** → sign up → **Domains → Add domain**. Copy the DNS records it shows into your domain's DNS settings → **Verify**.
2. **API Keys → Create** → Render's **RESEND_API_KEY**.
3. Set **EMAIL_FROM** to `Cramless <hello@your-domain.com>` and **SUPPORT_EMAIL** to an address you read.

Free plan: 100 emails a day. Morning emails are off by default (push notifications are the main reminder).

Until email is set up:
- With Google sign-in on, new accounts can only be created with Google, so nobody can claim someone else's email address.
- Without Google, email sign-up works, but addresses aren't verified.

Set up email before you invite the public: many students can't use Google sign-in until their school allows it. Once email is on, sign-ups that never confirm their address are removed after 48 hours.

## Step 7 (optional): Your own domain

1. Buy a domain (Cloudflare, Namecheap, …), about $10 a year.
2. Render → service → **Settings → Custom Domains** → add `app.your-domain.com`.
3. At your domain provider, add the **CNAME** record Render shows. If you use Cloudflare, set it to **DNS only** (grey cloud).
4. When Render shows the certificate as issued:
   - Set **PUBLIC_URL** to `https://app.your-domain.com`.
   - Update the two Google redirect URIs to match.

## Step 8: Check that everything works

- [ ] Sign up, finish setup, add demo data → the Today screen shows study sessions.
- [ ] Phone:
  - iPhone: open the site in Safari → **Share → Add to Home Screen** → open it from the home screen → Settings → **Turn on notifications** → **Send a test**.
  - Android: Chrome menu → **Install app**, then the same.
- [ ] cron-job.org shows successful runs (status 200).
- [ ] (With Google) Connect Google → your classes appear within a minute.
- [ ] (With AI) Courses → an exam → **Upload a file** → chapters are suggested.
- [ ] Settings → **Download my data** works. A test account can be deleted.

## Backups (recommended)

Supabase's free plan has no backups. A ready-made GitHub workflow (`.github/workflows/study-planner-backup.yml`) makes an encrypted backup every night and keeps 21 days of them.

1. Create an encryption key on your own computer.
   - Install **age** (github.com/FiloSottile/age → Releases).
   - Run `age-keygen -o cramless-backup-key.txt`.
   - Keep that file safe and offline. Without it, backups can't be opened.
   - The line starting with `age1…` is the public key.
2. GitHub → your repository → **Settings → Secrets and variables → Actions → New repository secret**:
   - `BACKUP_DATABASE_URL`: the same Supabase **Session pooler** string as in Render.
   - `BACKUP_AGE_PUBLIC_KEY`: the `age1…` line.
3. **Actions → Cramless database backup → Run workflow** to test it. Scheduled runs only start once this workflow is on your default branch (merge the branch). In a public repository, GitHub pauses scheduled workflows after 60 days with no activity.
4. Restoring:
   - Download the artifact.
   - `age -d -i cramless-backup-key.txt cramless-DATE.dump.age > backup.dump`
   - `pg_restore --no-owner -d "YOUR_DATABASE_URL" backup.dump`

## Updating the app

Every push to the branch deploys automatically. Database changes apply themselves on startup.

Every push also runs the tests (`.github/workflows/study-planner-ci.yml`), including against a real Postgres.

## When it grows

- **Render Starter (~$7/month):** no sleeping, faster.
- **Supabase Pro ($25/month):** no pausing, daily backups. On the free plan, use **Database → Backups**, or export your data yourself now and then.
- Watch the AI spend in the Anthropic Console, and raise the limits only when you're ready.
