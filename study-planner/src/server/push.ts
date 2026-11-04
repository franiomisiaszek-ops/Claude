import { eq } from 'drizzle-orm';
import webpush from 'web-push';
import type { DB } from './db';
import { pushSubscription } from './db/schema';
import { env, features } from './env';

let configured = false;
function configure() {
  if (configured || !features.push()) return;
  webpush.setVapidDetails(env.push.subject || `mailto:support@${new URL(env.publicUrl).hostname}`, env.push.publicKey!, env.push.privateKey!);
  configured = true;
}

export interface PushPayload {
  title: string;
  body: string;
  url?: string;
  tag?: string;
}

/** Sends to every device the user subscribed. Dead subscriptions are removed. Returns successes. */
export async function sendPush(db: DB, userId: string, payload: PushPayload): Promise<number> {
  if (!features.push()) return 0;
  configure();
  const subs = await db.select().from(pushSubscription).where(eq(pushSubscription.userId, userId));
  let ok = 0;
  await Promise.all(
    subs.map(async (sub) => {
      try {
        await webpush.sendNotification({ endpoint: sub.endpoint, keys: { p256dh: sub.p256dh, auth: sub.auth } }, JSON.stringify(payload), {
          TTL: 6 * 3600,
          urgency: 'normal',
          topic: payload.tag?.slice(0, 32),
        });
        ok++;
        await db.update(pushSubscription).set({ lastSuccessAt: new Date(), failureCount: 0 }).where(eq(pushSubscription.id, sub.id));
      } catch (e: any) {
        const status = e?.statusCode;
        if (status === 404 || status === 410 || sub.failureCount >= 5) await db.delete(pushSubscription).where(eq(pushSubscription.id, sub.id));
        else await db.update(pushSubscription).set({ failureCount: sub.failureCount + 1 }).where(eq(pushSubscription.id, sub.id));
      }
    }),
  );
  return ok;
}
