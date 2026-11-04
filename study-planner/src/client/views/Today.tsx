import { addDays, diffDays, fmtDate, fmtMin } from '../../shared/dates';
import { sessionTitle } from '../../shared/labels';
import type { PublicState } from '../../shared/types';
import { act, api } from '../api';
import { goTo } from '../App';
import { Card, ConfirmButton, DeadlineRunway, Empty, SessionCard } from '../components';

export function Today({ st }: { st: PublicState }) {
  const today = st.today;
  const blocked = st.blocks.some((b) => b.start <= today && today <= b.end);
  // On a blocked day the skipped sessions are just noise: the day-off card explains it.
  const sessions = st.sessions.filter((s) => s.date === today && s.status !== 'missed' && !(blocked && s.status === 'skipped'));
  const planned = sessions.filter((s) => s.status === 'planned');
  const checkedIn = sessions.some((s) => s.status === 'done' || s.status === 'partial');
  const doneMin = sessions.filter((s) => s.status === 'done' || s.status === 'partial').reduce((a, s) => a + (s.actualMinutes ?? s.minutes), 0);
  const plannedMin = planned.reduce((a, s) => a + s.minutes, 0);
  const next = st.sessions.find((s) => s.date > today && s.status === 'planned');
  const recentMissed = st.sessions.filter((s) => s.status === 'missed' && s.date >= addDays(today, -3)).length;
  const risky = st.planMeta.health.deadlines.filter((h) => h.shortfallMin > 0);
  const upcoming = st.planMeta.health.deadlines.filter((h) => h.dueAt <= addDays(today, 30)).sort((a, b) => a.dueAt.localeCompare(b.dueAt));
  const hasAnything = st.deadlines.length + st.materials.length > 0;

  return (
    <div className="stack">
      <div className="hero">
        <div>
          <p className="eyebrow">{fmtDate(today, { weekday: 'long', month: 'long', day: 'numeric' })}</p>
          <h1>{planned.length ? `${fmtMin(plannedMin)} of study today` : checkedIn ? 'All done for today 🎉' : 'Day off'}</h1>
          {doneMin > 0 && <p className="muted">{fmtMin(doneMin)} done so far. Nice.</p>}
        </div>
        {planned.length > 0 && (
          <ConfirmButton className="ghost" confirmText="Tap again to move today's study" onConfirm={() => act(api('POST', '/api/today/off'), "Today is now a day off. The plan moved everything to the coming days.")}>
            Can't study today
          </ConfirmButton>
        )}
      </div>

      {risky.length > 0 && (
        <div className="alert" role="alert">
          <strong>Heads up:</strong> {risky.map((h) => `${h.title} is short ${fmtMin(h.shortfallMin)}`).join('; ')}.{' '}
          <button className="link" onClick={() => goTo('plan')}>
            See fixes
          </button>
        </div>
      )}

      {sessions.length > 0 ? (
        <div className="stack-sm">
          {sessions.map((s) => (
            <SessionCard key={s.id} st={st} s={s} mode="today" />
          ))}
        </div>
      ) : (
        <Card className="dayoff">
          <div className="dayoff-art" aria-hidden>
            🌤️
          </div>
          <div>
            <h2>{blocked ? 'You blocked today, so enjoy it.' : hasAnything ? 'Nothing to study today.' : 'Your plan is empty.'}</h2>
            {next ? (
              <p>
                Next session: <strong>{fmtDate(next.date)}</strong>: {sessionTitle(st, next)} ({fmtMin(next.minutes)}).
              </p>
            ) : hasAnything ? (
              <p className="muted">No sessions are planned. Check your availability if that seems wrong.</p>
            ) : (
              <p>
                <button className="primary" onClick={() => goTo('sources')}>
                  Connect an app
                </button>{' '}
                or{' '}
                <button onClick={() => goTo('materials')}>add an exam yourself</button>
              </p>
            )}
          </div>
        </Card>
      )}

      {recentMissed > 0 && (
        <p className="muted small">
          ↻ {recentMissed} missed session{recentMissed > 1 ? 's were' : ' was'} spread over the coming days. No catch-up marathon needed.
        </p>
      )}

      {upcoming.length > 0 && (
        <Card title="Exam runway" actions={<button className="link" onClick={() => goTo('plan')}>Full plan →</button>}>
          <ul className="runways">
            {upcoming.map((h) => (
              <DeadlineRunway key={h.deadlineId} st={st} h={h} />
            ))}
          </ul>
          <p className="legend small muted">
            <span className="sw done" /> done <span className="sw planned" /> planned <span className="sw short" /> doesn't fit yet
          </p>
        </Card>
      )}

      <Week st={st} />
    </div>
  );
}

function Week({ st }: { st: PublicState }) {
  const days = Array.from({ length: 7 }, (_, i) => addDays(st.today, i + 1));
  return (
    <Card title="Next 7 days">
      <ol className="mini-week">
        {days.map((d) => {
          const ss = st.sessions.filter((s) => s.date === d && s.status === 'planned');
          const dl = st.deadlines.filter((x) => x.dueAt === d && x.status === 'active');
          const blocked = st.blocks.some((b) => b.start <= d && d <= b.end);
          const total = ss.reduce((a, s) => a + s.minutes, 0);
          return (
            <li key={d} className={blocked ? 'blocked' : ''}>
              <span className="mw-day">{fmtDate(d, { weekday: 'short' })}</span>
              <span className="mw-date">{fmtDate(d, { day: 'numeric' })}</span>
              <span className="mw-dots" aria-hidden>
                {ss.slice(0, 4).map((s) => (
                  <span key={s.id} className="dot" style={{ background: st.courses.find((c) => c.id === s.courseId)?.color }} />
                ))}
              </span>
              <span className="mw-total">{blocked ? 'off' : total ? fmtMin(total) : '-'}</span>
              {dl.map((x) => (
                <span key={x.id} className="mw-deadline" title={`${x.title} (${x.kind})`}>
                  🎯
                </span>
              ))}
              <span className="sr-only">
                {dl.map((x) => `${x.title} due; `)}
                {diffDays(st.today, d)} days from now
              </span>
            </li>
          );
        })}
      </ol>
    </Card>
  );
}
