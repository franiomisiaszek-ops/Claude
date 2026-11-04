import { useEffect, useState } from 'react';
import { APP_NAME, APP_TAGLINE } from '../../shared/brand';
import { refresh, toast } from '../api';
import { authClient, loadConfig, type PublicConfig } from '../auth-client';

type Mode = 'signin' | 'signup' | 'forgot' | 'reset' | 'check-email';

const errorText = (e: any) => e?.message || e?.statusText || 'Something went wrong. Please try again.';

export function Auth({ initial }: { initial?: Mode }) {
  const params = new URLSearchParams(location.search);
  const [cfg, setCfg] = useState<PublicConfig | null>(null);
  const [mode, setMode] = useState<Mode>(initial ?? (params.get('token') ? 'reset' : 'signin'));
  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [agree, setAgree] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(params.get('error') ? 'Sign-in was cancelled or failed. Please try again.' : null);

  useEffect(() => {
    void loadConfig().then(setCfg).catch(() => {});
  }, []);

  const done = async () => {
    await refresh().catch(() => {});
    history.replaceState(null, '', '/');
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (mode === 'signup') {
        if (!agree) throw new Error('Please confirm your age and accept the terms.');
        const { error } = await authClient.signUp.email({ email, password, name: name.trim() || email.split('@')[0], callbackURL: '/' });
        if (error) throw error;
        if (cfg?.emailVerification) setMode('check-email');
        else await done();
      } else if (mode === 'signin') {
        const { error } = await authClient.signIn.email({ email, password, callbackURL: '/' });
        if (error) {
          if (error.status === 403) throw new Error('Please confirm your email first. We sent you a link when you signed up.');
          throw error;
        }
        await done();
      } else if (mode === 'forgot') {
        const { error } = await authClient.requestPasswordReset({ email, redirectTo: `${location.origin}/` });
        if (error) throw error;
        toast('If that email has an account, a reset link is on its way.');
        setMode('signin');
      } else if (mode === 'reset') {
        const { error } = await authClient.resetPassword({ newPassword: password, token: params.get('token') ?? '' });
        if (error) throw error;
        toast('Password changed. Sign in with your new password.');
        history.replaceState(null, '', '/');
        setMode('signin');
      }
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const google = async () => {
    setBusy(true);
    const { error } = await authClient.signIn.social({ provider: 'google', callbackURL: '/', errorCallbackURL: '/?error=google' });
    if (error) {
      setError(errorText(error));
      setBusy(false);
    }
  };

  return (
    <div className="auth">
      <section className="auth-hero">
        <div className="brand big">
          <img src="/favicon.svg" alt="" width={44} height={44} />
          <span>{APP_NAME}</span>
        </div>
        <h1>{APP_TAGLINE}</h1>
        <ul className="auth-points">
          <li>
            <strong>Connect your classes.</strong> Google Classroom, Google Calendar, Canvas, Moodle, Blackboard, Brightspace and Outlook feeds, or type them in.
          </li>
          <li>
            <strong>Time comes from your material.</strong> Pages, slides, problems and lectures, adjusted for what's easy or hard for you.
          </li>
          <li>
            <strong>It learns from your grades.</strong> Tell it how each exam went and future plans fit you better.
          </li>
          <li>
            <strong>A reminder every morning,</strong> including on days off.
          </li>
        </ul>
      </section>

      <section className="auth-card card">
        {mode === 'check-email' ? (
          <div className="stack-sm">
            <h2>Check your email</h2>
            <p>
              We sent a confirmation link to <strong>{email}</strong>. Open it to finish creating your account.
            </p>
            <button className="link" onClick={() => setMode('signin')}>
              Back to sign in
            </button>
          </div>
        ) : (
          <>
            {(mode === 'signin' || mode === 'signup') && (
              <div className="segmented" role="tablist" aria-label="Account">
                <button role="tab" aria-selected={mode === 'signin'} className={mode === 'signin' ? 'active' : ''} onClick={() => setMode('signin')}>
                  Sign in
                </button>
                <button role="tab" aria-selected={mode === 'signup'} className={mode === 'signup' ? 'active' : ''} onClick={() => setMode('signup')}>
                  Create account
                </button>
              </div>
            )}
            {mode === 'forgot' && <h2>Reset your password</h2>}
            {mode === 'reset' && <h2>Choose a new password</h2>}

            {cfg?.googleSignIn && (mode === 'signin' || mode === 'signup') && (
              <>
                <button className="google-btn" onClick={google} disabled={busy}>
                  <GoogleMark /> Continue with Google
                </button>
                <div className="or">
                  <span>or</span>
                </div>
              </>
            )}

            <form className="stack-sm" onSubmit={submit}>
              {mode === 'signup' && (
                <label className="field">
                  Name
                  <input value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" placeholder="What should we call you?" />
                </label>
              )}
              {mode !== 'reset' && (
                <label className="field">
                  Email
                  <input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" />
                </label>
              )}
              {mode !== 'forgot' && (
                <label className="field">
                  {mode === 'reset' ? 'New password' : 'Password'}
                  <input
                    type="password"
                    required
                    minLength={10}
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    autoComplete={mode === 'signin' ? 'current-password' : 'new-password'}
                    placeholder={mode === 'signin' ? '' : 'At least 10 characters'}
                  />
                </label>
              )}
              {mode === 'signup' && (
                <label className="check-row small">
                  <input type="checkbox" checked={agree} onChange={(e) => setAgree(e.target.checked)} />
                  <span>
                    I'm at least 13 (or have a parent's permission) and I accept the <a href="/terms">Terms</a> and <a href="/privacy">Privacy Policy</a>.
                  </span>
                </label>
              )}
              {error && (
                <p className="error-text" role="alert">
                  {error}
                </p>
              )}
              <button className="primary big" disabled={busy}>
                {busy ? 'Please wait…' : mode === 'signin' ? 'Sign in' : mode === 'signup' ? 'Create account' : mode === 'forgot' ? 'Send reset link' : 'Change password'}
              </button>
            </form>

            <div className="auth-links small">
              {mode === 'signin' && cfg?.passwordReset && (
                <button className="link" onClick={() => setMode('forgot')}>
                  Forgot your password?
                </button>
              )}
              {(mode === 'forgot' || mode === 'reset') && (
                <button className="link" onClick={() => setMode('signin')}>
                  Back to sign in
                </button>
              )}
            </div>
          </>
        )}
        <p className="muted small legal-links">
          <a href="/privacy">Privacy</a> · <a href="/terms">Terms</a>
        </p>
      </section>
    </div>
  );
}

function GoogleMark() {
  return (
    <svg width="18" height="18" viewBox="0 0 48 48" aria-hidden>
      <path fill="#FFC107" d="M43.6 20.5H42V20H24v8h11.3C33.7 32.7 29.2 36 24 36c-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.8 1.2 7.9 3l5.7-5.7C34 6.1 29.3 4 24 4 12.9 4 4 12.9 4 24s8.9 20 20 20 20-8.9 20-20c0-1.3-.1-2.4-.4-3.5z" />
      <path fill="#FF3D00" d="M6.3 14.7l6.6 4.8C14.7 15.1 19 12 24 12c3.1 0 5.8 1.2 7.9 3l5.7-5.7C34 6.1 29.3 4 24 4 16.3 4 9.7 8.3 6.3 14.7z" />
      <path fill="#4CAF50" d="M24 44c5.2 0 9.9-2 13.4-5.2l-6.2-5.2C29.2 35.1 26.7 36 24 36c-5.2 0-9.6-3.3-11.3-8l-6.5 5C9.5 39.6 16.2 44 24 44z" />
      <path fill="#1976D2" d="M43.6 20.5H42V20H24v8h11.3c-.8 2.2-2.2 4.2-4.1 5.6l6.2 5.2C36.9 39.2 44 34 44 24c0-1.3-.1-2.4-.4-3.5z" />
    </svg>
  );
}
