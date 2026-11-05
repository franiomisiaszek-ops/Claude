/**
 * Fetches a user-supplied URL without letting it reach private networks (SSRF protection).
 * The IP check happens at connect time through a custom DNS lookup, so DNS rebinding can't bypass it.
 */
import dns from 'node:dns';
import http from 'node:http';
import https from 'node:https';
import net from 'node:net';

export class FetchBlockedError extends Error {}

function isPrivate(ip: string): boolean {
  if (net.isIPv4(ip)) {
    const [a, b] = ip.split('.').map(Number);
    return (
      a === 0 ||
      a === 10 ||
      a === 127 ||
      (a === 100 && b >= 64 && b <= 127) ||
      (a === 169 && b === 254) ||
      (a === 172 && b >= 16 && b <= 31) ||
      (a === 192 && b === 168) ||
      (a === 192 && b === 0) ||
      (a === 198 && (b === 18 || b === 19)) ||
      a >= 224
    );
  }
  const v6 = ip.toLowerCase();
  if (v6 === '::' || v6 === '::1') return true;
  if (v6.startsWith('::ffff:')) return isPrivate(v6.slice(7));
  return /^(fc|fd|fe8|fe9|fea|feb|ff)/.test(v6);
}

const guardedLookup: typeof dns.lookup = ((hostname: string, options: any, callback: any) => {
  const cb = typeof options === 'function' ? options : callback;
  const opts = typeof options === 'function' ? {} : options;
  dns.lookup(hostname, { ...opts, all: true }, (err, addresses: any) => {
    if (err) return cb(err);
    const list = (Array.isArray(addresses) ? addresses : [{ address: addresses, family: 4 }]) as { address: string; family: number }[];
    const bad = list.find((a) => isPrivate(a.address));
    if (bad || !list.length) return cb(new FetchBlockedError('That address points to a private network and is not allowed.'));
    if (opts.all) return cb(null, list);
    cb(null, list[0].address, list[0].family);
  });
}) as typeof dns.lookup;

export interface SafeResponse {
  status: number;
  headers: http.IncomingHttpHeaders;
  body: string;
}

/** Allow tests to fetch from localhost. Never set in production. */
const allowLocal = () => process.env.SAFE_FETCH_ALLOW_LOCAL === '1';

export function safeFetch(
  url: string,
  opts: { headers?: Record<string, string>; timeoutMs?: number; maxBytes?: number; redirects?: number } = {},
): Promise<SafeResponse> {
  const { timeoutMs = 20_000, maxBytes = 5_000_000, redirects = 3 } = opts;
  return new Promise((resolve, reject) => {
    let u: URL;
    try {
      u = new URL(url);
    } catch {
      return reject(new FetchBlockedError('That is not a valid URL.'));
    }
    if (u.protocol !== 'https:' && u.protocol !== 'http:') return reject(new FetchBlockedError('Only http and https links are allowed.'));
    if (u.username || u.password) return reject(new FetchBlockedError('Links with embedded passwords are not allowed.'));
    const lib = u.protocol === 'https:' ? https : http;
    const req = lib.request(
      u,
      {
        method: 'GET',
        headers: { 'User-Agent': 'CramlessCalendarSync/1.0', Accept: 'text/calendar, text/plain, */*', ...opts.headers },
        lookup: allowLocal() ? undefined : guardedLookup,
        timeout: timeoutMs,
      },
      (res) => {
        const status = res.statusCode ?? 0;
        if (status >= 300 && status < 400 && res.headers.location) {
          res.resume();
          if (redirects <= 0) return reject(new Error('Too many redirects.'));
          const next = new URL(res.headers.location, u).toString();
          return resolve(safeFetch(next, { ...opts, redirects: redirects - 1 }));
        }
        const chunks: Buffer[] = [];
        let size = 0;
        res.on('data', (c: Buffer) => {
          size += c.length;
          if (size > maxBytes) {
            req.destroy(new Error(`The response is larger than ${Math.round(maxBytes / 1e6)} MB.`));
            return;
          }
          chunks.push(c);
        });
        res.on('end', () => resolve({ status, headers: res.headers, body: Buffer.concat(chunks).toString('utf8') }));
        res.on('error', reject);
      },
    );
    req.on('timeout', () => req.destroy(new Error('The server took too long to answer.')));
    req.on('error', reject);
    req.end();
  });
}

export const __test = { isPrivate };
