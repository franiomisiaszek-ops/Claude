/** Startup checks and one-time server setup. */
import { eq } from 'drizzle-orm';
import webpush from 'web-push';
import { decrypt, encrypt } from './crypto';
import type { DB } from './db';
import { appSecret } from './db/schema';
import { env } from './env';

/** Fails fast with a clear message when production configuration is missing. */
export function assertProductionConfig(): void {
  if (!env.isProd) return;
  const problems: string[] = [];
  if (!process.env.PUBLIC_URL) problems.push('PUBLIC_URL (the https address of the site)');
  else if (!env.publicUrl.startsWith('https://')) problems.push('PUBLIC_URL must start with https://');
  if (!env.databaseUrl) problems.push('DATABASE_URL (your Postgres connection string)');
  if (!env.encryptionKey) problems.push('TOKEN_ENCRYPTION_KEY (32 random bytes, base64 or hex)');
  if (problems.length) throw new Error(`Missing configuration:\n  - ${problems.join('\n  - ')}`);
}

/** Uses VAPID keys from the environment, or generates a pair once and keeps it in the database. */
export async function ensurePushKeys(db: DB): Promise<void> {
  if (env.push.publicKey && env.push.privateKey) return;
  const [row] = await db.select().from(appSecret).where(eq(appSecret.key, 'vapid'));
  let keys: { publicKey: string; privateKey: string };
  if (row) keys = JSON.parse(decrypt(row.valueEnc));
  else {
    keys = webpush.generateVAPIDKeys();
    await db.insert(appSecret).values({ key: 'vapid', valueEnc: encrypt(JSON.stringify(keys)) }).onConflictDoNothing();
    const [again] = await db.select().from(appSecret).where(eq(appSecret.key, 'vapid'));
    if (again) keys = JSON.parse(decrypt(again.valueEnc));
  }
  env.push.publicKey = keys.publicKey;
  env.push.privateKey = keys.privateKey;
  if (!env.push.subject) env.push.subject = env.supportEmail ? `mailto:${env.supportEmail}` : env.publicUrl.startsWith('https://') ? env.publicUrl : 'mailto:admin@example.com';
}
