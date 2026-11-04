import { useEffect, useState } from 'react';
import { APP_NAME } from '../shared/brand';
import { act, api, refresh, setSignedOut, toast, useAppState, useToasts, type AppState } from './api';
import { authClient } from './auth-client';
import { clearDeviceData } from './push';
import { Auth } from './views/Auth';
import { Availability } from './views/Availability';
import { Inbox } from './views/Inbox';
import { Privacy, Terms } from './views/Legal';
import { Materials } from './views/Materials';
import { Onboarding } from './views/Onboarding';
import { Plan } from './views/Plan';
import { Progress } from './views/Progress';
import { Settings } from './views/Settings';
import { Sources } from './views/Sources';
import { Today } from './views/Today';

export type View = 'today' | 'plan' | 'materials' | 'progress' | 'inbox' | 'sources' | 'availability' | 'settings' | 'more';

const NAV: { id: View; label: string; icon: string; primary?: boolean }[] = [
  { id: 'today', label: 'Today', icon: '☀️', primary: true },
  { id: 'plan', label: 'Plan', icon: '🗓️', primary: true },
  { id: 'materials', label: 'Courses', icon: '📚', primary: true },
  { id: 'progress', label: 'Progress', icon: '📈', primary: true },
  { id: 'inbox', label: 'Inbox', icon: '📥' },
  { id: 'sources', label: 'Connected apps', icon: '🔌' },
  { id: 'availability', label: 'Availability', icon: '⏱️' },
  { id: 'settings', label: 'Settings', icon: '⚙️' },
];

const readView = (): View => {
  const v = new URLSearchParams(location.search).get('view') as View | null;
  return v && (NAV.some((n) => n.id === v) || v === 'more') ? v : 'today';
};

let navigate: (v: View) => void = () => {};
export const goTo = (v: View) => navigate(v);

export async function signOut() {
  await clearDeviceData();
  await authClient.signOut().catch(() => {});
  setSignedOut();
  history.replaceState(null, '', '/');
}

function DiffBanner({ st }: { st: AppState }) {
  const diff = st.planMeta.diff;
  const [dismissed, setDismissed] = useState<string | null>(null);
  const key = `${st.planMeta.plannedAt}|${diff?.summary}`;
  const undone = diff?.summary.startsWith('Undid');
  if (!diff || dismissed === key || (!undone && (!diff.cause || diff.added + diff.removed + diff.changed === 0))) return null;
  return (
    <div className="banner" role="status" aria-live="polite">
      <span>
        {undone ? (
          diff.summary
        ) : (
          <>
            <strong>Plan updated</strong> after “{diff.cause}”: {diff.summary}.
          </>
        )}
      </span>
      <span className="banner-actions">
        {!undone && st.canUndo === diff.cause && <button onClick={() => act(api('POST', '/api/undo'), 'Undone')}>Undo</button>}
        <button className="ghost" aria-label="Dismiss" onClick={() => setDismissed(key)}>
          ✕
        </button>
      </span>
    </div>
  );
}

function More({ st, go }: { st: AppState; go: (v: View) => void }) {
  return (
    <div className="stack">
      <h1>More</h1>
      <div className="more-list">
        {NAV.filter((n) => !n.primary).map((n) => (
          <button key={n.id} onClick={() => go(n.id)}>
            <span aria-hidden>{n.icon}</span> {n.label}
            {n.id === 'inbox' && st.inbox.length > 0 && <span className="badge">{st.inbox.length}</span>}
          </button>
        ))}
        <button onClick={() => void signOut()}>
          <span aria-hidden>↩</span> Sign out
        </button>
      </div>
    </div>
  );
}

export function App() {
  const path = location.pathname;
  if (path === '/privacy') return <Privacy />;
  if (path === '/terms') return <Terms />;
  return <Main />;
}

function Main() {
  const st = useAppState();
  const toasts = useToasts();
  const [view, setView] = useState<View>(readView);
  const [syncing, setSyncing] = useState(false);

  const go = (v: View) => {
    history.pushState(null, '', v === 'today' ? '/' : `/?view=${v}`);
    setView(v);
    scrollTo(0, 0);
  };
  navigate = go;

  useEffect(() => {
    void refresh().catch(() => {});
    const onPop = () => setView(readView());
    addEventListener('popstate', onPop);
    const t = setInterval(() => {
      if (document.visibilityState === 'visible') void refresh().catch(() => {});
    }, 120_000);
    const onVisible = () => document.visibilityState === 'visible' && void refresh().catch(() => {});
    document.addEventListener('visibilitychange', onVisible);
    const p = new URLSearchParams(location.search);
    if (p.get('error') && !p.get('token')) toast(p.get('error')!, 'error');
    if (p.get('warning')) toast(p.get('warning')!, 'error');
    if (p.get('connected')) toast('Google connected. Importing your classes…');
    if (p.get('connected') || (p.get('error') && p.get('view'))) history.replaceState(null, '', `/?view=${p.get('view') ?? 'today'}`);
    return () => {
      removeEventListener('popstate', onPop);
      clearInterval(t);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, []);

  if (st === undefined) return <div className="loading">Loading…</div>;
  if (st === null) return <Auth />;
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
          <span>{APP_NAME}</span>
        </div>
        <div className="topbar-actions">
          {st.sources.length > 0 && (
            <button className="ghost sync" onClick={syncAll} disabled={syncing} aria-label="Sync connected apps now">
              <span className={syncing ? 'spin' : ''} aria-hidden>
                ⟳
              </span>{' '}
              <span className="hide-sm">{syncing ? 'Syncing…' : 'Sync'}</span>
            </button>
          )}
          <button className="ghost hide-sm" onClick={() => void signOut()}>
            Sign out
          </button>
        </div>
      </header>
      <nav className="nav" aria-label="Main">
        {NAV.map((n) => (
          <button
            key={n.id}
            className={`${view === n.id ? 'active' : ''} ${n.primary ? '' : 'desktop-only'}`}
            aria-current={view === n.id ? 'page' : undefined}
            onClick={() => go(n.id)}
          >
            <span aria-hidden>{n.icon}</span>
            <span className="nav-label">{n.label}</span>
            {n.id === 'inbox' && inboxCount > 0 && <span className="badge">{inboxCount}</span>}
            {n.id === 'sources' && reconnect && <span className="badge warn">!</span>}
          </button>
        ))}
        <button className={`mobile-only ${view === 'more' || !NAV.find((n) => n.id === view)?.primary ? 'active' : ''}`} onClick={() => go('more')}>
          <span aria-hidden>☰</span>
          <span className="nav-label">More</span>
          {(inboxCount > 0 || reconnect) && <span className="badge">{inboxCount || '!'}</span>}
        </button>
      </nav>
      <main className="main">
        <DiffBanner st={st} />
        {view === 'today' && <Today st={st} />}
        {view === 'plan' && <Plan st={st} />}
        {view === 'materials' && <Materials st={st} />}
        {view === 'progress' && <Progress st={st} />}
        {view === 'inbox' && <Inbox st={st} />}
        {view === 'sources' && <Sources st={st} />}
        {view === 'availability' && <Availability st={st} />}
        {view === 'settings' && <Settings st={st} />}
        {view === 'more' && <More st={st} go={go} />}
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
