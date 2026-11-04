/**
 * All configuration comes from environment variables. Only PUBLIC_URL and BETTER_AUTH_SECRET
 * matter in production; everything else enables an optional feature when present.
 */
import crypto from 'node:crypto';

const isProd = process.env.NODE_ENV === 'production';
const port = Number(process.env.PORT ?? 3000);

function secret(name: string): string {
  const v = process.env[name];
  if (v && v.length >= 32) return v;
  if (isProd) throw new Error(`${name} must be set to a random string of at least 32 characters`);
  // Development only: a stable-per-process fallback so the app runs without setup.
  return crypto.createHash('sha256').update(`dev-${name}`).digest('hex');
}

export const env = {
  isProd,
  port,
  publicUrl: (process.env.PUBLIC_URL ?? `http://localhost:${port}`).replace(/\/$/, ''),
  /** Postgres connection string. Without it, an embedded Postgres (PGlite) stores data in DATA_DIR. */
  databaseUrl: process.env.DATABASE_URL,
  dataDir: process.env.DATA_DIR ?? 'data',
  authSecret: secret('BETTER_AUTH_SECRET'),
  /** 32-byte key (hex or base64) for encrypting Google tokens at rest. */
  encryptionKey: process.env.TOKEN_ENCRYPTION_KEY,
  google: {
    clientId: process.env.GOOGLE_CLIENT_ID,
    clientSecret: process.env.GOOGLE_CLIENT_SECRET,
  },
  anthropic: {
    apiKey: process.env.ANTHROPIC_API_KEY,
    model: process.env.ANTHROPIC_MODEL ?? 'claude-opus-5-5',
    /** Per-user daily limit on AI analyses, to protect the API budget. */
    dailyLimit: Number(process.env.AI_DAILY_LIMIT ?? 10),
    /** Whole-app monthly budget in US dollars; AI features pause when it's reached. */
    monthlyBudgetUsd: Number(process.env.AI_MONTHLY_BUDGET_USD ?? 4),
  },
  email: {
    resendApiKey: process.env.RESEND_API_KEY,
    from: process.env.EMAIL_FROM,
  },
  push: {
    publicKey: process.env.VAPID_PUBLIC_KEY,
    privateKey: process.env.VAPID_PRIVATE_KEY,
    subject: process.env.VAPID_SUBJECT,
  },
  /** Shown on the privacy policy and terms pages. */
  supportEmail: process.env.SUPPORT_EMAIL,
  /** Bearer token an external scheduler uses to call /api/cron/tick. */
  cronSecret: process.env.CRON_SECRET,
  /** Run background jobs inside the web process (on by default). */
  inProcessJobs: process.env.IN_PROCESS_JOBS !== 'false',
};

export const features = {
  google: () => !!(env.google.clientId && env.google.clientSecret),
  ai: () => !!env.anthropic.apiKey,
  email: () => !!(env.email.resendApiKey && env.email.from),
  push: () => !!(env.push.publicKey && env.push.privateKey),
};
