import { useEffect, useState } from 'react';
import type { Source } from '../../shared/types';
import { act, api, toast, type AppState } from '../api';
import { Card, ConfirmButton, relTime } from '../components';

const TYPE_LABEL: Record<Source['type'], string> = {
  demo: 'Demo data',
  ics: 'Calendar feed',
  'google-classroom': 'Google Classroom',
  'google-calendar': 'Google Calendar',
};

export function Sources({ st }: { st: AppState }) {
  return (
    <div className="stack">
      <div className="page-head">
        <h1>Connected apps</h1>
      </div>
      <p className="muted">
        Connected apps are checked in the background (Classroom every 30 min, Calendar every 15 min, feeds every 3 h) and when you press Sync. Clear new items go
        straight into the plan; anything uncertain waits in your Inbox.
      </p>
      {st.sources.length > 0 && (
        <div className="stack-sm">
          {st.sources.map((s) => (
            <SourceRow key={s.id} st={st} s={s} />
          ))}
        </div>
      )}
      <GoogleCard st={st} />
      <IcsForm />
      {!st.sources.some((s) => s.type === 'demo') && (
        <Card title="Just trying it out?">
          <p className="muted">Load a sample term with four courses, exams, a quiz, an essay and reading material. Remove it any time.</p>
          <button onClick={() => act(api('POST', '/api/sources', { type: 'demo' }), 'Demo data added')}>Add demo data</button>
        </Card>
      )}
    </div>
  );
}

function SourceRow({ st, s }: { st: AppState; s: Source }) {
  const [busy, setBusy] = useState(false);
  const items = st.deadlines.filter((d) => d.origin?.sourceId === s.id).length + st.materials.filter((m) => m.origin?.sourceId === s.id).length;
  return (
    <Card className={`source-row ${s.status}`}>
      <div className="sr-main">
        <div>
          <strong>{s.name}</strong> <span className="chip">{TYPE_LABEL[s.type]}</span>
        </div>
        <div className="small muted">
          Last checked {relTime(s.lastSyncAt)} · {items} item{items === 1 ? '' : 's'}
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
        <ConfirmButton
          className="ghost danger"
          confirmText="Remove app and its imported items?"
          onConfirm={() => act(s.type.startsWith('google') ? api('POST', '/api/google/disconnect', {}) : api('DELETE', `/api/sources/${s.id}`), 'Removed')}
        >
          Remove
        </ConfirmButton>
      </div>
      {s.type === 'google-calendar' && !s.needsReconnect && st.google.connected && <CalendarPicker s={s} />}
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
      <p className="small muted">Events in these calendars count as busy time inside your study window. Ones that look like exams are suggested as deadlines.</p>
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

function GoogleCard({ st }: { st: AppState }) {
  const connected = st.google.connected && st.sources.some((s) => s.type.startsWith('google'));
  return (
    <Card title="Google Classroom & Google Calendar">
      {!st.google.available ? (
        <p className="muted">Google connection isn't switched on for this site yet. Use your school's calendar feed below, or add exams yourself.</p>
      ) : connected ? (
        <p>
          ✅ Connected{st.google.email ? ` as ${st.google.email}` : ''}. Classroom coursework, tests and materials and your calendar's busy times are read (never
          changed). <a href="/oauth/google/start">Reconnect</a>
        </p>
      ) : (
        <>
          <p className="muted">Read-only access: we import your Classroom coursework and materials, and see busy times in the calendars you pick. We never post or change anything.</p>
          <a className="button primary" href="/oauth/google/start">
            Connect Google
          </a>
          <p className="small muted">
            School accounts: if you're under 18, your school's Google admin has to allow this app first. If Google blocks the connection, use the calendar feed
            below; Classroom puts due dates in Google Calendar, so its feed covers most of it.
          </p>
        </>
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
        <summary>Where do I find my feed link?</summary>
        <ul className="small">
          <li>
            <strong>Canvas:</strong> Calendar → “Calendar Feed” (bottom right) → copy the link.
          </li>
          <li>
            <strong>Moodle:</strong> Calendar → “Export calendar” → “All courses”, “Recent and next 60 days” → “Get calendar URL”.
          </li>
          <li>
            <strong>Blackboard:</strong> Calendar → settings ⚙ → “Share calendar” → copy the link.
          </li>
          <li>
            <strong>Brightspace (D2L):</strong> Calendar → “Subscribe” → copy the link.
          </li>
          <li>
            <strong>Outlook / Teams:</strong> Outlook settings → Calendar → Shared calendars → “Publish a calendar” → copy the ICS link (choose “Personal
            calendar” below).
          </li>
          <li>
            <strong>Google Calendar:</strong> Calendar settings → “Secret address in iCal format”.
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
          Feed link <input required value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://… or webcal://…" inputMode="url" />
        </label>
        <label>
          Name <input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Canvas" />
        </label>
        <label>
          This feed is my…
          <select value={feedType} onChange={(e) => setFeedType(e.target.value as 'lms' | 'calendar')}>
            <option value="lms">School deadlines</option>
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
