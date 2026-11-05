import { useMemo, useState } from 'react';
import { addDays, dateRange, fmtDate, fmtMin, weekday } from '../../shared/dates';
import { materialName } from '../../shared/labels';
import type { ISODate } from '../../shared/types';
import { act, api, type AppState } from '../api';
import { Card, DeadlineRunway, Empty, SessionCard } from '../components';

type Mode = 'agenda' | 'week' | 'month';

const savedMode = (): Mode => {
  try {
    return (localStorage.getItem('planMode') as Mode) || 'agenda';
  } catch {
    return 'agenda';
  }
};

export function Plan({ st }: { st: AppState }) {
  const [mode, setModeState] = useState<Mode>(savedMode);
  const [anchor, setAnchor] = useState(st.today);
  const setMode = (m: Mode) => {
    setModeState(m);
    try {
      localStorage.setItem('planMode', m);
    } catch {}
  };
  const health = st.planMeta.health;

  return (
    <div className="stack">
      <div className="page-head">
        <h1>Study plan</h1>
        <div className="segmented" role="tablist" aria-label="View">
          {(['agenda', 'week', 'month'] as Mode[]).map((m) => (
            <button key={m} role="tab" aria-selected={mode === m} className={mode === m ? 'active' : ''} onClick={() => setMode(m)}>
              {m[0].toUpperCase() + m.slice(1)}
            </button>
          ))}
        </div>
      </div>

      {health.status !== 'ok' && (
        <Card title={health.status === 'atRisk' ? '⚠️ Some deadlines don’t fit yet' : 'Tight schedule'} className={health.status === 'atRisk' ? 'risk' : 'tight'}>
          <p className="muted small">The planner never crams silently. Here is what is short and what would fix it.</p>
          <ul className="runways">
            {health.deadlines
              .filter((h) => h.shortfallMin > 0 || h.bufferShrunk)
              .map((h) => (
                <DeadlineRunway key={h.deadlineId} st={st} h={h} />
              ))}
          </ul>
        </Card>
      )}

      {mode === 'agenda' && <Agenda st={st} />}
      {mode === 'week' && <Week st={st} anchor={anchor} setAnchor={setAnchor} />}
      {mode === 'month' && (
        <Month
          st={st}
          anchor={anchor}
          setAnchor={setAnchor}
          openWeek={(d) => {
            setAnchor(d);
            setMode('week');
          }}
        />
      )}

      <p className="muted small">
        Edits pin a session so re-planning leaves it alone. Removing a session puts its time back into the plan.{' '}
        <a href="/api/export.ics" download>
          Download .ics
        </a>{' '}
        to see the plan in your own calendar app.
      </p>
    </div>
  );
}

function useDayInfo(st: AppState) {
  return useMemo(() => {
    const blocked = (d: ISODate) => st.blocks.some((b) => b.start <= d && d <= b.end);
    const capacity = (d: ISODate) => (blocked(d) ? 0 : Math.floor((st.availability.minutesByWeekday[weekday(d)] ?? 0) * st.settings.slack));
    return { blocked, capacity };
  }, [st.blocks, st.availability, st.settings.slack]);
}

function AddSession({ st, date }: { st: AppState; date: ISODate }) {
  const [open, setOpen] = useState(false);
  const [materialId, setMaterialId] = useState('');
  const [minutes, setMinutes] = useState('30');
  const options = [
    ...st.materials.filter((m) => !m.done).map((m) => ({ id: m.id, label: `${st.courses.find((c) => c.id === m.courseId)?.name ?? ''} · ${m.title}` })),
    ...st.deadlines.filter((d) => d.status === 'active' && !st.materials.some((m) => m.deadlineId === d.id)).map((d) => ({ id: `auto:${d.id}`, label: `${st.courses.find((c) => c.id === d.courseId)?.name ?? ''} · ${materialName(st, `auto:${d.id}`)}` })),
  ];
  if (!open) {
    return (
      <button className="link small" onClick={() => setOpen(true)} disabled={!options.length}>
        + Add session
      </button>
    );
  }
  return (
    <form
      className="inline add-session"
      onSubmit={async (e) => {
        e.preventDefault();
        if (await act(api('POST', '/api/sessions', { date, materialId: materialId || options[0].id, minutes: Number(minutes) }))) setOpen(false);
      }}
    >
      <select value={materialId} onChange={(e) => setMaterialId(e.target.value)} aria-label="What to study">
        {options.map((o) => (
          <option key={o.id} value={o.id}>
            {o.label}
          </option>
        ))}
      </select>
      <input type="number" min={5} max={600} step={5} value={minutes} onChange={(e) => setMinutes(e.target.value)} aria-label="Minutes" />
      <button className="primary">Add</button>
      <button type="button" className="ghost" onClick={() => setOpen(false)}>
        Cancel
      </button>
    </form>
  );
}

function Agenda({ st }: { st: AppState }) {
  const { blocked, capacity } = useDayInfo(st);
  const lastDue = st.deadlines.reduce((a, d) => (d.dueAt > a ? d.dueAt : a), addDays(st.today, 13));
  const days = dateRange(st.today, lastDue > addDays(st.today, 90) ? addDays(st.today, 90) : lastDue);
  const visible = days.filter((d) => st.sessions.some((s) => s.date === d && s.status !== 'missed') || st.deadlines.some((x) => x.dueAt === d) || blocked(d) || d === st.today);
  if (!st.sessions.length && !st.deadlines.length) return <Empty>No plan yet. Add an exam or connect an app to get started.</Empty>;
  return (
    <ol className="agenda">
      {visible.map((d) => {
        const ss = st.sessions.filter((s) => s.date === d && s.status !== 'missed');
        const total = ss.filter((s) => s.status === 'planned').reduce((a, s) => a + s.minutes, 0);
        const cap = capacity(d);
        const dls = st.deadlines.filter((x) => x.dueAt === d);
        return (
          <li key={d} className={`agenda-day ${blocked(d) ? 'blocked' : ''}`}>
            <div className="agenda-date">
              <span className="ad-wd">{d === st.today ? 'Today' : fmtDate(d, { weekday: 'short' })}</span>
              <span className="ad-d">{fmtDate(d, { month: 'short', day: 'numeric' })}</span>
              {!blocked(d) && (
                <span className="ad-load" title={`${total} of ${cap} available minutes`}>
                  {fmtMin(total)}
                  {cap ? ` / ${fmtMin(cap)}` : ''}
                </span>
              )}
            </div>
            <div className="agenda-items">
              {blocked(d) && <div className="blocked-note">Blocked: {st.blocks.find((b) => b.start <= d && d <= b.end)?.reason || 'day off'}</div>}
              {dls.map((x) => (
                <div key={x.id} className="deadline-pill" style={{ ['--c' as string]: st.courses.find((c) => c.id === x.courseId)?.color }}>
                  🎯 {x.title} <span className="chip">{x.kind}</span>
                  {x.status === 'done' && <span className="chip status-done">done</span>}
                </div>
              ))}
              {ss.map((s) => (
                <SessionCard key={s.id} st={st} s={s} mode={s.status === 'planned' ? 'edit' : 'compact'} />
              ))}
              {!blocked(d) && <AddSession st={st} date={d} />}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

function Week({ st, anchor, setAnchor }: { st: AppState; anchor: ISODate; setAnchor: (d: ISODate) => void }) {
  const { blocked, capacity } = useDayInfo(st);
  const start = addDays(anchor, -((weekday(anchor) + 6) % 7)); // Monday
  const days = dateRange(start, addDays(start, 6));
  const [dragOver, setDragOver] = useState<ISODate | null>(null);
  return (
    <Card
      title={`${fmtDate(days[0], { month: 'short', day: 'numeric' })} – ${fmtDate(days[6], { month: 'short', day: 'numeric' })}`}
      actions={
        <>
          <button className="ghost" aria-label="Previous week" onClick={() => setAnchor(addDays(start, -7))}>
            ←
          </button>
          <button className="ghost" onClick={() => setAnchor(st.today)}>
            This week
          </button>
          <button className="ghost" aria-label="Next week" onClick={() => setAnchor(addDays(start, 7))}>
            →
          </button>
        </>
      }
    >
      <p className="muted small">Drag a session to another day to move it (it gets pinned). On a keyboard, use the agenda's date field.</p>
      <div className="week-grid">
        {days.map((d) => {
          const ss = st.sessions.filter((s) => s.date === d && s.status !== 'missed');
          const total = ss.filter((s) => s.status === 'planned').reduce((a, s) => a + s.minutes, 0);
          const cap = capacity(d);
          const past = d < st.today;
          return (
            <div
              key={d}
              className={`week-col ${blocked(d) ? 'blocked' : ''} ${d === st.today ? 'today' : ''} ${dragOver === d ? 'over' : ''} ${past ? 'past' : ''}`}
              onDragOver={(e) => {
                if (past) return;
                e.preventDefault();
                setDragOver(d);
              }}
              onDragLeave={() => setDragOver(null)}
              onDrop={(e) => {
                e.preventDefault();
                setDragOver(null);
                const id = e.dataTransfer.getData('text/plain');
                if (id && !past) void act(api('PUT', `/api/sessions/${id}`, { date: d }));
              }}
            >
              <div className="week-head">
                <span>{fmtDate(d, { weekday: 'short' })}</span>
                <strong>{fmtDate(d, { day: 'numeric' })}</strong>
                <span className="small muted">{blocked(d) ? 'off' : `${fmtMin(total)}${cap ? `/${fmtMin(cap)}` : ''}`}</span>
                <span className="loadbar" aria-hidden>
                  <span style={{ width: `${cap ? Math.min(100, (total / cap) * 100) : 0}%` }} />
                </span>
              </div>
              {st.deadlines
                .filter((x) => x.dueAt === d)
                .map((x) => (
                  <div key={x.id} className="deadline-pill small" style={{ ['--c' as string]: st.courses.find((c) => c.id === x.courseId)?.color }}>
                    🎯 {x.title}
                  </div>
                ))}
              {ss.map((s) => (
                <div key={s.id} draggable={s.status === 'planned'} onDragStart={(e) => e.dataTransfer.setData('text/plain', s.id)} className="draggable">
                  <SessionCard st={st} s={s} mode="compact" />
                </div>
              ))}
            </div>
          );
        })}
      </div>
    </Card>
  );
}

function Month({ st, anchor, setAnchor, openWeek }: { st: AppState; anchor: ISODate; setAnchor: (d: ISODate) => void; openWeek: (d: ISODate) => void }) {
  const { blocked, capacity } = useDayInfo(st);
  const first = `${anchor.slice(0, 7)}-01`;
  const gridStart = addDays(first, -((weekday(first) + 6) % 7));
  const days = dateRange(gridStart, addDays(gridStart, 41));
  const month = anchor.slice(0, 7);
  const nextMonth = addDays(`${month}-28`, 7).slice(0, 7) + '-01';
  const prevMonth = addDays(first, -1).slice(0, 7) + '-01';
  return (
    <Card
      title={fmtDate(first, { month: 'long', year: 'numeric' })}
      actions={
        <>
          <button className="ghost" aria-label="Previous month" onClick={() => setAnchor(prevMonth)}>
            ←
          </button>
          <button className="ghost" onClick={() => setAnchor(st.today)}>
            Today
          </button>
          <button className="ghost" aria-label="Next month" onClick={() => setAnchor(nextMonth)}>
            →
          </button>
        </>
      }
    >
      <div className="month-grid" role="grid">
        {['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map((w) => (
          <div key={w} className="month-wd" role="columnheader">
            {w}
          </div>
        ))}
        {days.map((d) => {
          const total = st.sessions.filter((s) => s.date === d && s.status !== 'missed' && s.status !== 'skipped').reduce((a, s) => a + s.minutes, 0);
          const cap = capacity(d);
          const ratio = cap ? Math.min(1, total / cap) : total ? 1 : 0;
          const dls = st.deadlines.filter((x) => x.dueAt === d);
          return (
            <button
              key={d}
              role="gridcell"
              className={`month-cell ${d.slice(0, 7) !== month ? 'other' : ''} ${blocked(d) ? 'blocked' : ''} ${d === st.today ? 'today' : ''}`}
              style={{ ['--load' as string]: ratio }}
              onClick={() => openWeek(d)}
              aria-label={`${fmtDate(d)}: ${total ? fmtMin(total) : 'nothing'} planned${dls.length ? `, ${dls.map((x) => x.title).join(', ')} due` : ''}${blocked(d) ? ', blocked' : ''}`}
            >
              <span className="mc-num">{Number(d.slice(8))}</span>
              {total > 0 && <span className="mc-min">{fmtMin(total)}</span>}
              {dls.map((x) => (
                <span key={x.id} className="mc-dl" style={{ background: st.courses.find((c) => c.id === x.courseId)?.color }} title={x.title}>
                  {x.kind === 'exam' ? 'Exam' : x.kind === 'quiz' ? 'Quiz' : 'Due'}
                </span>
              ))}
            </button>
          );
        })}
      </div>
      <p className="legend small muted">Darker = more of that day's available time is used. Click a day to open its week.</p>
    </Card>
  );
}
