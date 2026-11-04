import crypto from 'node:crypto';
import { env } from './env';

/** 32-byte key from TOKEN_ENCRYPTION_KEY (hex or base64), or derived from the auth secret with HKDF. */
function loadKey(): Buffer {
  const raw = env.encryptionKey?.trim();
  if (raw) {
    if (/^[0-9a-f]{64}$/i.test(raw)) return Buffer.from(raw, 'hex');
    const b = Buffer.from(raw, 'base64');
    if (b.length === 32) return b;
    throw new Error('TOKEN_ENCRYPTION_KEY must be 32 bytes, as 64 hex characters or base64');
  }
  return Buffer.from(crypto.hkdfSync('sha256', env.authSecret, 'cramless', 'token-encryption-v1', 32));
}

let key: Buffer | null = null;
const getKey = () => (key ??= loadKey());

/** AES-256-GCM. Output: `v1.<base64url(iv | tag | ciphertext)>`. */
export function encrypt(plain: string): string {
  const iv = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv('aes-256-gcm', getKey(), iv);
  const ct = Buffer.concat([cipher.update(plain, 'utf8'), cipher.final()]);
  return `v1.${Buffer.concat([iv, cipher.getAuthTag(), ct]).toString('base64url')}`;
}

export function decrypt(blob: string): string {
  if (!blob.startsWith('v1.')) throw new Error('Unknown ciphertext version');
  const buf = Buffer.from(blob.slice(3), 'base64url');
  const decipher = crypto.createDecipheriv('aes-256-gcm', getKey(), buf.subarray(0, 12));
  decipher.setAuthTag(buf.subarray(12, 28));
  return Buffer.concat([decipher.update(buf.subarray(28)), decipher.final()]).toString('utf8');
}

/** Encrypted, expiring token for OAuth `state` (keeps the PKCE verifier secret and binds the user). */
export function seal(payload: Record<string, unknown>, ttlSeconds: number): string {
  return encrypt(JSON.stringify({ ...payload, exp: Date.now() + ttlSeconds * 1000 }));
}

export function unseal<T extends Record<string, unknown>>(token: string): T {
  const data = JSON.parse(decrypt(token)) as T & { exp: number };
  if (typeof data.exp !== 'number' || data.exp < Date.now()) throw new Error('This link has expired. Please try again.');
  return data;
}

export const randomId = (prefix = '', bytes = 12) => `${prefix}${crypto.randomBytes(bytes).toString('base64url')}`;
