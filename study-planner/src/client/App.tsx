import { useEffect, useState } from 'react';
import { hhmmToMin, localParts } from '../shared/dates';
import type { PublicState } from '../shared/types';
import { act, api, refresh, toast, useAppState, useToasts } from './api';
import { Availability } from './views/Availability';
import { Inbox } from './views/Inbox';
import { Materials } from './views/Materials';
import { Onboarding } from './views/Onboarding';
import { Plan } from './views/Plan';
import { Settings } from './views/Settings';
import { Sources } from './views/Sources';
import { Today } from './views/Today';

export type View = 'today' | 'plan' | 'materials' | 'inbox' | 'sources' | 'availability' | 'settings';

const NAV: { id: View; label: string; short?: string; icon: string }[] = [
  { id: 'today', label: 'Today', icon: '☀️' },
  { id: 'plan', label: 'Plan', icon: '🗓️' },
  { id: 'materials', label: 'Courses', icon: '📚' },
  { id: 'inbox', label: 'Inbox', icon: '📥' },
  { id: 'sources', label: 'Apps', icon: '🔌' },
  { id: 'availability', label: 'Availability', short: 'Hours', icon: '⏱️' },
  { id: 'settings', label: 'Settings', short: 'More', icon: '⚙️' },
];

const readView = (): View => {
  const v = new URLSearchParams(location.search).get('view') as View | null;
  return v && NAV.some((n) => n.id === v) ? v : 'today';
};

export function useNav() {
  const [view, setView] = useState<View>(readView);
  useEffect(() => {
    const onPop = () => setView(readView());
    addEventListener('popstate', onPop);
    return () => removeEventListener('popstate', onPop);
  }, []);
  const go = (v: View) => {
    history.pushState(null, '', v === 'today' ? '/' : `/?view=${v}`);
    setView(v);
    scrollTo(0, 0);
  };
  return { view, go };
}

let navigate: (v: View) => void = () => {};
export const goTo = (v: View) => navigate(v);

/** Shows the daily digest as a browser notification once a day while the app is open. */
function useDailyNotification(st: PublicState | null) {
  useEffect(() => {
    if (!st || typeof Notification === 'undefined') return;
    const check = async () => {
      if (Notification.permission !== 'granted') return;
      const now = localParts(st.settings.timezone);
      const key = `notified:${now.date}`;
      if (now.minutes < hhmmToMin(st.settings.digestTime)) return;
      try {
        if (localStorage.getItem(key)) return;
        localStorage.setItem(key, '1');
      } catch {
        return;
      }
      const d = await api<{ subject: string; text: string }>('GET', '/api/digest');
      new Notification(`📚 ${d.subject}`, { body: d.text.split('\n').slice(0, 4).join('\n'), icon: '/favicon.svg', tag: 'daily-digest' });
      void api('POST', '/api/digest/browser');
    };
    void check();
    const t = setInterval(check, 60_000);
    return () => clearInterval(t);
  }, [st?.settings.digestTime, st?.settings.timezone]);
}

function DiffBanner({ st }: { st: PublicState }) {
  const diff = st.planMeta.diff;
  const [dismissed, setDismissed] = useState<string | null>(null);
  const key = `${st.planMeta.plannedAt}|${diff?.summary}`;
  const undone = diff?.summary.startsWith('Undid');
  if (!diff || dismissed === key || (!undone && (!diff.cause || diff.added + diff.removed + diff.changed === 0))) return null;
  return (
    <div className="banner" role="status" aria-live="polite">
      <span>{undone ? diff.summary : <><strong>Plan updated</strong> after “{diff.cause}”: {diff.summary}.</>}</span>
      <span className="banner-actions">
        {!undone && st.canUndo === diff.cause && <button onClick={() => act(api('POST', '/api/undo'), 'Undone')}>Undo</button>}
        <button className="ghost" aria-label="Dismiss" onClick={() => setDismissed(key)}>
          ✕
        </button>
      </span>
    </div>
  );
}

export function App() {
  const st = useAppState();
  const toasts = useToasts();
  const { view, go } = useNav();
  navigate = go;
  const [syncing, setSyncing] = useState(false);

  useEffect(() => {
    void refresh().catch((e) => toast(`Can't reach the server: ${e.message}`, 'error'));
    const t = setInterval(() => void refresh().catch(() => {}), 60_000);
    const p = new URLSearchParams(location.search);
    if (p.get('error')) toast(p.get('error')!, 'error');
    if (p.get('connected')) toast('Google connected. Importing your classes…');
    if (p.get('error') || p.get('connected')) history.replaceState(null, '', `/?view=${p.get('view') ?? 'today'}`);
    return () => clearInterval(t);
  }, []);
  useDailyNotification(st);

  if (!st) return <div className="loading">Loading your plan…</div>;
  if (!st.settings.onboarded) return <Onboarding st={st} />;

  const inboxCount = st.inbox.length;
  const reconnect = st.sources.some((s) => s.needsReconnect);
  const syncAll = async () => {
    setSyncing(true);
    await act(api('POST', '/api/sync'), 'Synced');
    setSyncing(false);
  };

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <img src="/favicon.svg" alt="" width={28} height={28} />
          <span>Study Planner</span>
        </div>
        {st.sources.length > 0 && (
          <button className="ghost sync" onClick={syncAll} disabled={syncing} aria-label="Sync connected apps now">
            <span className={syncing ? 'spin' : ''} aria-hidden>
              ⟳
            </span>{' '}
            {syncing ? 'Syncing…' : 'Sync'}
          </button>
        )}
      </header>
      <nav className="nav" aria-label="Main">
        {NAV.map((n) => (
          <button key={n.id} className={view === n.id ? 'active' : ''} aria-current={view === n.id ? 'page' : undefined} onClick={() => go(n.id)}>
            <span aria-hidden>{n.icon}</span>
            <span className="nav-label">{n.label}</span>
            {n.short && (
              <span className="nav-short" aria-hidden>
                {n.short}
              </span>
            )}
            {n.id === 'inbox' && inboxCount > 0 && <span className="badge">{inboxCount}</span>}
            {n.id === 'sources' && reconnect && <span className="badge warn">!</span>}
          </button>
        ))}
      </nav>
      <main className="main">
        <DiffBanner st={st} />
        {view === 'today' && <Today st={st} />}
        {view === 'plan' && <Plan st={st} />}
        {view === 'materials' && <Materials st={st} />}
        {view === 'inbox' && <Inbox st={st} />}
        {view === 'sources' && <Sources st={st} />}
        {view === 'availability' && <Availability st={st} />}
        {view === 'settings' && <Settings st={st} />}
      </main>
      <div className="toasts" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast ${t.kind}`} role={t.kind === 'error' ? 'alert' : 'status'}>
            {t.text}
          </div>
        ))}
      </div>
    </div>
  );
}
