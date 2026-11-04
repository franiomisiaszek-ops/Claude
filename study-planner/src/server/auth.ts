import { betterAuth } from 'better-auth';
import { drizzleAdapter } from 'better-auth/adapters/drizzle';
import { APP_NAME } from '../shared/brand';
import type { DB } from './db';
import { account, rateLimit, session, user, verification } from './db/schema';
import { env, features } from './env';
import { sendEmail } from './email';

export function createAuth(db: DB, hooks: { onUserCreated: (userId: string) => Promise<void> }) {
  return betterAuth({
    appName: APP_NAME,
    baseURL: env.publicUrl,
    secret: env.authSecret,
    // In development the Vite dev server (port 5173) proxies to the API.
    trustedOrigins: env.isProd ? [env.publicUrl] : [env.publicUrl, 'http://localhost:5173'],
    database: drizzleAdapter(db, { provider: 'pg', schema: { user, session, account, verification, rateLimit } }),
    emailAndPassword: {
      enabled: true,
      minPasswordLength: 10,
      maxPasswordLength: 128,
      // Verification needs a working email sender; without one, accounts work unverified.
      requireEmailVerification: features.email(),
      // Without an email sender, addresses can't be verified. If Google sign-in exists, use only that for new
      // accounts, so nobody can claim someone else's address with an unverified account.
      disableSignUp: !features.email() && features.google(),
      autoSignIn: true,
      sendResetPassword: async ({ user: u, url }) => {
        await sendEmail({
          to: u.email,
          subject: `Reset your ${APP_NAME} password`,
          text: `Someone asked to reset the password for your ${APP_NAME} account.\n\nReset it here (valid for 1 hour):\n${url}\n\nIf this wasn't you, ignore this email.`,
        });
      },
    },
    emailVerification: {
      sendOnSignUp: features.email(),
      autoSignInAfterVerification: true,
      sendVerificationEmail: async ({ user: u, url }) => {
        await sendEmail({
          to: u.email,
          subject: `Confirm your ${APP_NAME} email`,
          text: `Welcome to ${APP_NAME}!\n\nConfirm your email address:\n${url}\n\nIf you didn't sign up, ignore this email.`,
        });
      },
    },
    socialProviders: features.google()
      ? { google: { clientId: env.google.clientId!, clientSecret: env.google.clientSecret!, prompt: 'select_account' } }
      : {},
    rateLimit: {
      enabled: true,
      storage: 'database',
      window: 60,
      max: 100,
      customRules: {
        '/sign-in/email': { window: 60, max: 5 },
        '/sign-up/email': { window: 3600, max: 10 },
        '/forget-password': { window: 3600, max: 5 },
        '/request-password-reset': { window: 3600, max: 5 },
      },
    },
    session: {
      expiresIn: 60 * 60 * 24 * 30,
      updateAge: 60 * 60 * 24,
    },
    advanced: {
      useSecureCookies: env.publicUrl.startsWith('https://'),
      // Express resolves the client address (with `trust proxy`) and passes it in this header,
      // so rate limits stay per user even when the proxy chain adds several addresses.
      ipAddress: { ipAddressHeaders: ['x-cramless-client-ip'] },
    },
    databaseHooks: {
      user: {
        create: {
          after: async (u) => {
            await hooks.onUserCreated(u.id);
          },
        },
      },
    },
  });
}

export type Auth = ReturnType<typeof createAuth>;
