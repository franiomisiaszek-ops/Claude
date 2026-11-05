import { env, features } from './env';

export interface EmailMessage {
  to: string;
  subject: string;
  text: string;
  html?: string;
}

/** Sends through Resend's HTTP API. Without configuration, logs instead (development). */
export async function sendEmail(msg: EmailMessage): Promise<void> {
  if (!features.email()) {
    if (!env.isProd) console.log(`[email:dev] to=${msg.to} subject="${msg.subject}"\n${msg.text}\n`);
    return;
  }
  const res = await fetch('https://api.resend.com/emails', {
    method: 'POST',
    headers: { Authorization: `Bearer ${env.email.resendApiKey}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ from: env.email.from, to: [msg.to], subject: msg.subject, text: msg.text, html: msg.html }),
    signal: AbortSignal.timeout(15_000),
  });
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new Error(`Email provider error ${res.status}: ${body.slice(0, 200)}`);
  }
}
