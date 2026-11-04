import { useState } from 'react';
import type { PublicState } from '../../shared/types';
import { act, api, toast } from '../api';
import { Card, ConfirmButton } from '../components';

const zones = (() => {
  try {
    return (Intl as any).supportedValuesOf('timeZone') as string[];
  } catch {
    return [];
  }
})();

export function Settings({ st }: { st: PublicState }) {
  const s = st.settings;
  const [tz, setTz] = useState(s.timezone);
  const [digestTime, setDigestTime] = useState(s.digestTime);
  const [perm, setPerm] = useState(typeof Notification === 'undefined' ? 'unsupported' : Notification.permission);
  const [tune, setTune] = useState({ slack: Math.round(s.slack * 100), minChunk: s.minChunk, maxSession: s.maxSession, maxSubjects: s.maxSubjects });
  const [email, setEmail] = useState({ host: s.email?.host ?? '', port: s.email?.port ?? 587, secure: s.email?.secure ?? false, user: s.email?.user ?? '', pass: '', from: s.email?.from ?? '', to: s.email?.to ?? '' });
  const lastEmail = st.digestLog.filter((l) => l.channel === 'email').at(-1);

  return (
    <div className="stack">
      <div className="page-head">
        <h1>Settings</h1>
      </div>

      <Card title="Daily reminder">
        <form
          className="form-grid"
          onSubmit={(e) => {
            e.preventDefault();
            void act(api('PUT', '/api/settings', { timezone: tz, digestTime }), 'Saved');
          }}
        >
          <label>
            Remind me at <input type="time" value={digestTime} onChange={(e) => setDigestTime(e.target.value)} />
          </label>
          <label className="grow">
            Time zone
            <input list="tz-list" value={tz} onChange={(e) => setTz(e.target.value)} />
            <datalist id="tz-list">
              {zones.map((z) => (
                <option key={z} value={z} />
              ))}
            </datalist>
          </label>
          <div className="form-actions">
            <button className="primary">Save</button>
          </div>
        </form>
        <div className="reminder-channels">
          <div>
            <strong>Browser notification</strong>
            <p className="small muted">Shows today's plan (or “day off”) at the reminder time while this app is open in a tab.</p>
            {perm === 'granted' ? (
              <button
                onClick={async () => {
                  const d = await api<{ subject: string; text: string }>('GET', '/api/digest');
                  new Notification(`📚 ${d.subject}`, { body: d.text, icon: '/favicon.svg' });
                }}
              >
                Send a test notification
              </button>
            ) : perm === 'unsupported' ? (
              <p className="small">Not supported in this browser.</p>
            ) : (
              <button onClick={async () => setPerm(await Notification.requestPermission())}>{perm === 'denied' ? 'Blocked in browser settings' : 'Enable notifications'}</button>
            )}
          </div>
          <div>
            <strong>Calendar app</strong>
            <p className="small muted">Import the plan into Google/Apple/Outlook calendar to get its alerts on your phone. Re-download after big changes.</p>
            <a className="button" href="/api/export.ics" download>
              Download .ics
            </a>
          </div>
        </div>
      </Card>

      <Card title="Email digest (optional)">
        <p className="small muted">
          Sent every morning at the reminder time after syncing your apps, through your own SMTP account (e.g. Gmail with an app password: smtp.gmail.com,
          port 465, secure). The app must be running.
          {lastEmail && ` Last sent ${new Date(lastEmail.sentAt).toLocaleString()}.`}
        </p>
        <form
          className="form-grid"
          onSubmit={(e) => {
            e.preventDefault();
            void act(api('PUT', '/api/settings', { email }), 'Email settings saved');
          }}
        >
          <label className="grow">
            SMTP host <input value={email.host} onChange={(e) => setEmail({ ...email, host: e.target.value })} placeholder="smtp.example.com" />
          </label>
          <label>
            Port <input type="number" value={email.port} onChange={(e) => setEmail({ ...email, port: Number(e.target.value) })} />
          </label>
          <label className="check-row">
            <input type="checkbox" checked={email.secure} onChange={(e) => setEmail({ ...email, secure: e.target.checked })} /> TLS (port 465)
          </label>
          <label className="grow">
            Username <input value={email.user} onChange={(e) => setEmail({ ...email, user: e.target.value })} autoComplete="off" />
          </label>
          <label className="grow">
            Password
            <input type="password" value={email.pass} onChange={(e) => setEmail({ ...email, pass: e.target.value })} placeholder={s.email?.hasPass ? '(unchanged)' : ''} autoComplete="new-password" />
          </label>
          <label className="grow">
            Send to <input type="email" value={email.to} onChange={(e) => setEmail({ ...email, to: e.target.value })} />
          </label>
          <div className="form-actions">
            <button className="primary">Save</button>
            {s.email && (
              <>
                <button type="button" onClick={() => act(api('POST', '/api/digest/email'), 'Test email sent')}>
                  Send test
                </button>
                <button type="button" className="ghost danger" onClick={() => act(api('PUT', '/api/settings', { email: null }), 'Email turned off')}>
                  Turn off
                </button>
              </>
            )}
          </div>
        </form>
      </Card>

      <Card title="Planner tuning">
        <form
          className="form-grid"
          onSubmit={(e) => {
            e.preventDefault();
            void act(api('PUT', '/api/settings', { ...tune, slack: tune.slack / 100 }), 'Saved. Plan updated.');
          }}
        >
          <label>
            Fill available time to (%) <input type="number" min={50} max={100} value={tune.slack} onChange={(e) => setTune({ ...tune, slack: Number(e.target.value) })} />
          </label>
          <label>
            Shortest session (min) <input type="number" min={10} max={60} value={tune.minChunk} onChange={(e) => setTune({ ...tune, minChunk: Number(e.target.value) })} />
          </label>
          <label>
            Longest session (min) <input type="number" min={20} max={180} value={tune.maxSession} onChange={(e) => setTune({ ...tune, maxSession: Number(e.target.value) })} />
          </label>
          <label>
            Max subjects per day <input type="number" min={1} max={6} value={tune.maxSubjects} onChange={(e) => setTune({ ...tune, maxSubjects: Number(e.target.value) })} />
          </label>
          <div className="form-actions">
            <button className="primary">Save</button>
          </div>
        </form>
      </Card>

      <Card title="Data">
        <p className="small muted">Everything is stored locally in the app's data folder on this computer. Nothing is sent anywhere except to the apps you connect.</p>
        <ConfirmButton
          className="danger"
          confirmText="Really erase everything?"
          onConfirm={async () => {
            await act(api('POST', '/api/reset'));
            toast('All data erased');
          }}
        >
          Erase all data
        </ConfirmButton>
      </Card>
    </div>
  );
}
