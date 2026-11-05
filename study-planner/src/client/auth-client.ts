import { createAuthClient } from 'better-auth/react';

export const authClient = createAuthClient({ baseURL: window.location.origin });

export interface PublicConfig {
  appName: string;
  googleSignIn: boolean;
  emailSignUp: boolean;
  googleClientId: string | null;
  supportEmail: string | null;
  emailVerification: boolean;
  passwordReset: boolean;
}

export async function loadConfig(): Promise<PublicConfig> {
  const res = await fetch('/api/config');
  return res.json();
}
