import { useEffect, useState } from 'react';
import { fmtDate, fmtMin, WEEKDAYS } from '../../shared/dates';
import type { PublicState } from '../../shared/types';
import { act, api } from '../api';
import { Card, Empty } from '../components';

const ORDER = [1, 2, 3, 4, 5, 6, 0]; // Monday first

export function WeeklyEditor({ value, onChange }: { value: number[]; onChange: (v: number[]) => void }) {
  return (
    <div className="weekly">
      {ORDER.map((wd) => (
        <label key={wd} className="weekly-day">
          <span className="wd">{WEEKDAYS[wd]}</span>
          <input
            type="range"
            min={0}
            max={360}
            step={15}
            value={value[wd]}
            aria-valuetext={value[wd] ? fmtMin(value[wd]) : 'Day off'}
            onChange={(e) => {
              const next = [...value];
              next[wd] = Number(e.target.value);
              onChange(next);
            }}
          />
          <span className="wv">{value[wd] ? fmtMin(value[wd]) : 'off'}</span>
        </label>
      ))}
    </div>
  );
}

export function Availability({ st }: { st: PublicState }) {
  const [weekly, setWeekly] = useState(st.availability.minutesByWeekday);
  const [useWindow, setUseWindow] = useState(!!st.availability.studyWindow);
  const [win, setWin] = useState(st.availability.studyWindow ?? { start: '16:00', end: '20:00' });
  useEffect(() => setWeekly(st.availability.minutesByWeekday), [st.availability.minutesByWeekday.join(',')]);
  const dirty = weekly.join(',') !== st.availability.minutesByWeekday.join(',') || useWindow !== !!st.availability.studyWindow || (useWindow && JSON.stringify(win) !== JSON.stringify(st.availability.studyWindow));
  const total = weekly.reduce((a, b) => a + b, 0);

  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  const [reason, setReason] = useState('');
  const blocks = [...st.blocks].filter((b) => b.end >= st.today).sort((a, b) => a.start.localeCompare(b.start));

  return (
    <div className="stack">
      <div className="page-head">
        <h1>Availability</h1>
      </div>
      <Card title="Usual week" actions={<span className="muted small">{fmtMin(total)} / week</span>}>
        <p className="muted small">How long you can study on each day. The planner fills about {Math.round(st.settings.slack * 100)}% of this, so a slow day doesn't wreck the plan.</p>
        <WeeklyEditor value={weekly} onChange={setWeekly} />
        <label className="check-row">
          <input type="checkbox" checked={useWindow} onChange={(e) => setUseWindow(e.target.checked)} />
          I usually study between set hours (busy events from your calendar in that window reduce study time)
        </label>
        {useWindow && (
          <div className="form-grid">
            <label>
              From <input type="time" value={win.start} onChange={(e) => setWin({ ...win, start: e.target.value })} />
            </label>
            <label>
              To <input type="time" value={win.end} onChange={(e) => setWin({ ...win, end: e.target.value })} />
            </label>
          </div>
        )}
        <div className="form-actions">
          <button
            className="primary"
            disabled={!dirty}
            onClick={() => act(api('PUT', '/api/availability', { minutesByWeekday: weekly, studyWindow: useWindow ? win : null }), 'Availability saved. Plan updated.')}
          >
            Save
          </button>
        </div>
      </Card>

      <Card title="Days you can't study">
        <form
          className="form-grid"
          onSubmit={async (e) => {
            e.preventDefault();
            if (await act(api('POST', '/api/blocks', { start, end: end || start, reason }), 'Blocked. Sessions moved to other days.')) {
              setStart('');
              setEnd('');
              setReason('');
            }
          }}
        >
          <label>
            From <input type="date" required min={st.today} value={start} onChange={(e) => setStart(e.target.value)} />
          </label>
          <label>
            To (optional) <input type="date" min={start || st.today} value={end} onChange={(e) => setEnd(e.target.value)} />
          </label>
          <label className="grow">
            Reason (optional) <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="e.g. Family trip" />
          </label>
          <div className="form-actions">
            <button className="primary">Block</button>
          </div>
        </form>
        {blocks.length ? (
          <ul className="blocks">
            {blocks.map((b) => (
              <li key={b.id}>
                <span>
                  <strong>{b.start === b.end ? fmtDate(b.start) : `${fmtDate(b.start)} – ${fmtDate(b.end)}`}</strong>
                  {b.reason && <span className="muted"> · {b.reason}</span>}
                </span>
                <button className="ghost small" onClick={() => act(api('DELETE', `/api/blocks/${b.id}`), 'Unblocked')}>
                  Remove
                </button>
              </li>
            ))}
          </ul>
        ) : (
          <Empty>No blocked days coming up.</Empty>
        )}
      </Card>
    </div>
  );
}
