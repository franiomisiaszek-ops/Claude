import { useEffect, useState } from 'react';
import { GRADE_SCALES } from '../../shared/grades';
import type { GradeValue } from '../../shared/types';
import { act, api, setSignedOut, toast, type AppState } from '../api';
import { GradeInput, Card } from '../components';
import { clearDeviceData, disablePush, enablePush, isStandalone, pushStatus, type PushStatus } from '../push';
import { signOut } from '../App';

const zones = (() => {
  try {
    return (Intl as any).supportedValuesOf('timeZone') as string[];
  } catch {
    return [];
  }
})();

export function Settings({ st }: { st: AppState }) {
  const s = st.settings;
  const [tz, setTz] = useState(s.timezone);
  const [digestTime, setDigestTime] = useState(s.digestTime);
  const [scale, setScale] = useState(s.gradeScale);
  const [target, setTarget] = useState<GradeValue | undefined>(s.targetGrade);
  const [tune, setTune] = useState({ slack: Math.round(s.slack * 100), minChunk: s.minChunk, maxSession: s.maxSession, maxSubjects: s.maxSubjects });

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
        <PushSettings st={st} />
        {st.features.email && (
          <label className="check-row">
            <input type="checkbox" checked={s.reminders.email} onChange={(e) => act(api('PUT', '/api/settings', { reminders: { ...s.reminders, email: e.target.checked } }))} />
            Also email me the daily plan ({st.user.email}){!st.user.emailVerified && ' · confirm your email address first'}
          </label>
        )}
        <p className="small muted">
          Prefer your own calendar app? <a href="/api/export.ics">Download the plan as .ics</a> and import it (re-download after big changes).
        </p>
      </Card>

      <Card title="Grades">
        <p className="small muted">Used when you tell the app how an exam went. Each course can have its own target in Progress.</p>
        <form
          className="form-grid"
          onSubmit={(e) => {
            e.preventDefault();
            void act(api('PUT', '/api/settings', { gradeScale: scale, ...(target ? { targetGrade: target } : {}) }), 'Saved');
          }}
        >
          <label className="grow">
            My grading scale
            <select value={scale} onChange={(e) => (setScale(e.target.value), setTarget(undefined))}>
              {GRADE_SCALES.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.label}
                  {g.hint ? ` (${g.hint})` : ''}
                </option>
              ))}
            </select>
          </label>
          <label>
            Usual target
            <GradeInput scaleId={scale} value={target} onChange={setTarget} />
          </label>
          <div className="form-actions">
            <button className="primary">Save</button>
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

      <Account st={st} />
    </div>
  );
}

function PushSettings({ st }: { st: AppState }) {
  const [status, setStatus] = useState<PushStatus | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    void pushStatus().then(setStatus);
  }, []);
  if (!st.features.push) return <p className="small muted">Push reminders aren't switched on for this site yet.</p>;
  if (!status) return null;
  return (
    <div className="push-settings">
      <strong>Notifications on this device</strong>
      {status === 'needs-install' && (
        <p className="small">
          On iPhone and iPad, add the app to your Home Screen first: tap Share, then “Add to Home Screen”, then open it from there and turn notifications on.
        </p>
      )}
      {status === 'unsupported' && <p className="small muted">This browser doesn't support push notifications.</p>}
      {status === 'denied' && <p className="small">Notifications are blocked for this site. Allow them in your browser's site settings, then reload.</p>}
      {(status === 'off' || status === 'on') && (
        <div className="form-actions">
          {status === 'off' ? (
            <button
              className="primary"
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                try {
                  await enablePush(st.vapidPublicKey!);
                  await act(api('PUT', '/api/settings', { reminders: { ...st.settings.reminders, push: true } }));
                  setStatus('on');
                  toast('Notifications are on for this device.');
                } catch (e) {
                  toast((e as Error).message, 'error');
                }
                setBusy(false);
              }}
            >
              Turn on notifications
            </button>
          ) : (
            <>
              <span className="chip status-done">On for this device</span>
              <button onClick={() => act(api('POST', '/api/push/test'), 'Test sent')}>Send a test</button>
              <button
                className="ghost"
                onClick={async () => {
                  await disablePush();
                  setStatus('off');
                }}
              >
                Turn off here
              </button>
            </>
          )}
        </div>
      )}
      {!isStandalone() && status !== 'needs-install' && <p className="small muted">Tip: install the app (browser menu → Install, or Add to Home Screen) for a full-screen experience.</p>}
    </div>
  );
}

function Account({ st }: { st: AppState }) {
  const [confirm, setConfirm] = useState('');
  return (
    <Card title="Account">
      <p>
        Signed in as <strong>{st.user.name}</strong> ({st.user.email})
      </p>
      <div className="form-actions">
        <a className="button" href="/api/account/export" download>
          Download my data
        </a>
        <button onClick={() => void signOut()}>Sign out</button>
      </div>
      <details className="danger-zone">
        <summary>Delete account</summary>
        <p className="small">This permanently deletes your account and all your data, and disconnects Google. It can't be undone.</p>
        <form
          className="inline"
          onSubmit={async (e) => {
            e.preventDefault();
            const ok = await act(api('DELETE', '/api/account', { confirm }));
            if (ok) {
              await clearDeviceData();
              setSignedOut();
              history.replaceState(null, '', '/');
            }
          }}
        >
          <label className="small">
            Type DELETE to confirm <input value={confirm} onChange={(e) => setConfirm(e.target.value)} aria-label="Type DELETE to confirm" />
          </label>
          <button className="danger" disabled={confirm !== 'DELETE'}>
            Delete my account
          </button>
        </form>
      </details>
      <p className="small muted">
        <a href="/privacy">Privacy Policy</a> · <a href="/terms">Terms</a>
      </p>
    </Card>
  );
}
