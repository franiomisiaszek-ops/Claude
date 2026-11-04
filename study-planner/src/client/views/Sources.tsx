import { useEffect, useState } from 'react';
import type { PublicState, Source } from '../../shared/types';
import { act, api, toast } from '../api';
import { Card, ConfirmButton, relTime } from '../components';

const TYPE_LABEL: Record<Source['type'], string> = {
  demo: 'Demo data',
  ics: 'Calendar feed (ICS)',
  'google-classroom': 'Google Classroom',
  'google-calendar': 'Google Calendar',
};

export function Sources({ st }: { st: PublicState }) {
  return (
    <div className="stack">
      <div className="page-head">
        <h1>Connected apps</h1>
      </div>
      <p className="muted">
        Connected apps are checked in the background (Classroom every 30 min, Calendar every 15 min, feeds every 3 h) and whenever you press Sync. New exams and
        material are added to the plan automatically.
      </p>
      {st.sources.length > 0 && (
        <div className="stack-sm">
          {st.sources.map((s) => (
            <SourceRow key={s.id} st={st} s={s} />
          ))}
        </div>
      )}
      <GoogleSetup st={st} />
      <IcsForm />
      {!st.sources.some((s) => s.type === 'demo') && (
        <Card title="Just trying it out?">
          <p className="muted">Load a sample school term with four courses, exams, a quiz, an essay and reading material. Remove it any time.</p>
          <button onClick={() => act(api('POST', '/api/sources', { type: 'demo' }), 'Demo data added')}>Add demo data</button>
        </Card>
      )}
    </div>
  );
}

function SourceRow({ st, s }: { st: PublicState; s: Source }) {
  const [busy, setBusy] = useState(false);
  const items = st.deadlines.filter((d) => d.origin?.sourceId === s.id).length + st.materials.filter((m) => m.origin?.sourceId === s.id).length;
  return (
    <Card className={`source-row ${s.status}`}>
      <div className="sr-main">
        <div>
          <strong>{s.name}</strong> <span className="chip">{TYPE_LABEL[s.type]}</span>
        </div>
        <div className="small muted">
          {s.status === 'syncing' ? 'Syncing…' : `Last checked ${relTime(s.lastSyncAt)}`} · {items} item{items === 1 ? '' : 's'}
          {s.lastChangeAt ? ` · last change ${relTime(s.lastChangeAt)}` : ''}
        </div>
        {s.error && <div className="error-text small">⚠ {s.error}</div>}
      </div>
      <div className="sr-actions">
        {s.needsReconnect ? (
          <a className="button primary" href="/oauth/google/start">
            Reconnect
          </a>
        ) : (
          <button
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              await act(api('POST', `/api/sources/${s.id}/sync`));
              setBusy(false);
            }}
          >
            {busy ? 'Syncing…' : 'Sync now'}
          </button>
        )}
        <ConfirmButton className="ghost danger" confirmText="Remove app and its imported items?" onConfirm={() => act(api('DELETE', `/api/sources/${s.id}`), 'Removed')}>
          Remove
        </ConfirmButton>
      </div>
      {s.type === 'google-calendar' && !s.needsReconnect && st.googleConnected && <CalendarPicker s={s} />}
    </Card>
  );
}

function CalendarPicker({ s }: { s: Source }) {
  const [open, setOpen] = useState(false);
  const [cals, setCals] = useState<{ id: string; summary: string; primary: boolean }[] | null>(null);
  const [selected, setSelected] = useState<string[]>(s.config.calendarIds ?? ['primary']);
  useEffect(() => {
    if (!open || cals) return;
    api<{ id: string; summary: string; primary: boolean }[]>('GET', '/api/google/calendars')
      .then(setCals)
      .catch((e) => toast(e.message, 'error'));
  }, [open]);
  if (!open)
    return (
      <button className="link small" onClick={() => setOpen(true)}>
        Choose calendars to read…
      </button>
    );
  return (
    <div className="calendar-picker">
      <p className="small muted">Events in these calendars count as busy time (inside your study window). Ones that look like exams are suggested as deadlines.</p>
      {!cals && <p className="small">Loading…</p>}
      {cals?.map((c) => {
        const id = c.primary ? 'primary' : c.id;
        return (
          <label key={c.id} className="check-row">
            <input type="checkbox" checked={selected.includes(id)} onChange={(e) => setSelected(e.target.checked ? [...selected, id] : selected.filter((x) => x !== id))} />
            {c.summary}
            {c.primary ? ' (main)' : ''}
          </label>
        );
      })}
      <button
        className="primary"
        onClick={async () => {
          await act(api('PUT', `/api/sources/${s.id}`, { calendarIds: selected }));
          await act(api('POST', `/api/sources/${s.id}/sync`), 'Calendars updated');
          setOpen(false);
        }}
      >
        Save
      </button>
    </div>
  );
}

function GoogleSetup({ st }: { st: PublicState }) {
  const [clientId, setClientId] = useState(st.settings.googleClient?.clientId ?? '');
  const [secret, setSecret] = useState('');
  const [show, setShow] = useState(!st.settings.googleClient);
  const hasClient = !!st.settings.googleClient;
  const connected = st.googleConnected && st.sources.some((s) => s.type.startsWith('google'));
  return (
    <Card title="Google Classroom & Google Calendar">
      {connected ? (
        <p>
          ✅ Connected. Classroom assignments, tests and materials, plus your calendar's busy time, are imported read-only.{' '}
          <a href="/oauth/google/start">Reconnect</a>
        </p>
      ) : (
        <>
          <p className="muted">
            Google requires your own (free) OAuth client so this app can read your classes privately. It takes about 5 minutes, once.
          </p>
          {hasClient && (
            <p>
              <a className="button primary" href="/oauth/google/start">
                Connect Google
              </a>{' '}
              <button className="link" onClick={() => setShow(!show)}>
                {show ? 'Hide setup' : 'Change client'}
              </button>
            </p>
          )}
        </>
      )}
      {show && !connected && (
        <div className="google-setup">
          <ol className="steps">
            <li>
              Open the <a href="https://console.cloud.google.com/projectcreate" target="_blank" rel="noreferrer">Google Cloud console</a> and create a project.
            </li>
            <li>
              Enable the <a href="https://console.cloud.google.com/apis/library/classroom.googleapis.com" target="_blank" rel="noreferrer">Classroom API</a> and the{' '}
              <a href="https://console.cloud.google.com/apis/library/calendar-json.googleapis.com" target="_blank" rel="noreferrer">Calendar API</a>.
            </li>
            <li>
              Under <em>Google Auth Platform → Branding/Audience</em>, set up an <strong>External</strong> app and add yourself as a user. Then click{' '}
              <strong>Publish app</strong> (“In production”). In “Testing” mode Google signs you out every 7 days. You'll see an “unverified app” warning
              when connecting; that's expected for a personal app.
            </li>
            <li>
              Under <em>Clients</em>, create an OAuth client of type <strong>Web application</strong> with this authorized redirect URI:
              <code className="copy" onClick={() => navigator.clipboard?.writeText(st.redirectUri).then(() => toast('Copied'))} title="Click to copy">
                {st.redirectUri}
              </code>
            </li>
            <li>Paste the client ID and secret here, save, then press Connect.</li>
          </ol>
          <form
            className="form-grid"
            onSubmit={async (e) => {
              e.preventDefault();
              if (await act(api('PUT', '/api/google/client', { clientId, clientSecret: secret }), 'Saved. Now press “Connect Google”.')) {
                setSecret('');
                setShow(false);
              }
            }}
          >
            <label className="grow">
              Client ID <input required value={clientId} onChange={(e) => setClientId(e.target.value)} placeholder="1234-abc.apps.googleusercontent.com" />
            </label>
            <label className="grow">
              Client secret
              <input type="password" required={!hasClient} value={secret} onChange={(e) => setSecret(e.target.value)} placeholder={hasClient ? '(unchanged)' : 'GOCSPX-…'} autoComplete="off" />
            </label>
            <div className="form-actions">
              <button className="primary">Save</button>
            </div>
          </form>
          <p className="small muted">
            School accounts: some schools block apps their admin hasn't approved. If Google says access is blocked, use your school's calendar feed below
            instead. Classroom adds every due date to Google Calendar too, so the Calendar feed covers most of it.
          </p>
        </div>
      )}
    </Card>
  );
}

function IcsForm() {
  const [url, setUrl] = useState('');
  const [name, setName] = useState('');
  const [feedType, setFeedType] = useState<'lms' | 'calendar'>('lms');
  const [busy, setBusy] = useState(false);
  return (
    <Card title="School calendar feed (Canvas, Moodle, Blackboard, Brightspace, Teams/Outlook…)">
      <details className="help">
        <summary>Where do I find my feed URL?</summary>
        <ul className="small">
          <li>
            <strong>Canvas:</strong> Calendar → “Calendar Feed” (bottom right) → copy the link.
          </li>
          <li>
            <strong>Moodle:</strong> Calendar → “Export calendar” → choose “All courses” and “Recent and next 60 days” → “Get calendar URL”.
          </li>
          <li>
            <strong>Blackboard:</strong> Calendar → settings ⚙ → “Share calendar” → copy the link.
          </li>
          <li>
            <strong>Brightspace (D2L):</strong> Calendar → “Subscribe” → copy the link.
          </li>
          <li>
            <strong>Outlook / Teams:</strong> Outlook settings → Calendar → Shared calendars → “Publish a calendar” → copy the ICS link. Choose “Personal
            calendar” below.
          </li>
          <li>
            <strong>Google Calendar (without OAuth):</strong> Calendar settings → “Secret address in iCal format”.
          </li>
        </ul>
      </details>
      <form
        className="form-grid"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          const ok = await act(api('POST', '/api/sources', { type: 'ics', url, name: name || 'Calendar feed', feedType }), 'Feed added and synced');
          setBusy(false);
          if (ok) {
            setUrl('');
            setName('');
          }
        }}
      >
        <label className="grow">
          Feed URL <input required value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://… or webcal://…" inputMode="url" />
        </label>
        <label>
          Name <input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Canvas" />
        </label>
        <label>
          This feed is my…
          <select value={feedType} onChange={(e) => setFeedType(e.target.value as 'lms' | 'calendar')}>
            <option value="lms">School deadlines (LMS)</option>
            <option value="calendar">Personal calendar (busy time + exams)</option>
          </select>
        </label>
        <div className="form-actions">
          <button className="primary" disabled={busy}>
            {busy ? 'Adding…' : 'Add feed'}
          </button>
        </div>
      </form>
    </Card>
  );
}
