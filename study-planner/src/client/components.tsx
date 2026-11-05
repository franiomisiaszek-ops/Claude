import { useEffect, useRef, useState, type ReactNode } from 'react';
import { diffDays, fmtDate, fmtMin } from '../shared/dates';
import { explain } from '../shared/explain';
import { scaleById } from '../shared/grades';
import { sessionWhat } from '../shared/labels';
import type { DeadlineHealth, GradeValue, Session } from '../shared/types';
import { act, api, type AppState } from './api';

export const courseOf = (st: AppState, id: string) => st.courses.find((c) => c.id === id);

export function CourseTag({ st, id }: { st: AppState; id: string }) {
  const c = courseOf(st, id);
  return (
    <span className="course-tag">
      <span className="dot" style={{ background: c?.color ?? 'var(--muted)' }} aria-hidden />
      {c?.name ?? 'Course'}
    </span>
  );
}

const STATUS_LABEL: Record<Session['status'], string> = { planned: 'Planned', done: 'Done', partial: 'Partly done', skipped: 'Skipped', missed: 'Missed' };

const setSession = (id: string, body: Record<string, unknown>) => act(api('PUT', `/api/sessions/${id}`, body));

/** One study session. On the Today screen it has check-in actions; elsewhere it's editable. */
export function SessionCard({ st, s, mode }: { st: AppState; s: Session; mode: 'today' | 'edit' | 'compact' }) {
  const [why, setWhy] = useState(false);
  const [partial, setPartial] = useState(false);
  const [mins, setMins] = useState(String(Math.round(s.minutes / 2)));
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [, tick] = useState(0);
  useEffect(() => {
    if (!startedAt) return;
    const t = setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, [startedAt]);
  const course = courseOf(st, s.courseId);
  const deadline = s.deadlineId ? st.deadlines.find((d) => d.id === s.deadlineId) : undefined;
  const elapsed = startedAt ? Math.floor((Date.now() - startedAt) / 1000) : 0;
  const finished = s.status !== 'planned';
  const practice = s.materialId.startsWith('practice:');

  return (
    <article className={`session ${s.status} ${mode}`} style={{ ['--c' as string]: course?.color ?? 'var(--accent)' }}>
      <div className="session-main">
        <div className="session-head">
          <CourseTag st={st} id={s.courseId} />
          {s.kind === 'review' && <span className="chip">Review</span>}
          {practice && <span className="chip">Practice</span>}
          {s.pinned && <span className="chip" title="You placed this session; re-planning won't move it">📌 Pinned</span>}
          {finished && <span className={`chip status-${s.status}`}>{STATUS_LABEL[s.status]}</span>}
        </div>
        <div className="session-title">{sessionWhat(st, s)}</div>
        <div className="session-meta">
          <strong>{fmtMin(s.minutes)}</strong>
          {s.actualMinutes != null && <span> · took {fmtMin(s.actualMinutes)}</span>}
          {deadline && mode !== 'compact' && <span> · for {deadline.title} ({fmtDate(deadline.dueAt, { month: 'short', day: 'numeric' })})</span>}
          {mode !== 'compact' && (
            <button className="link" aria-expanded={why} onClick={() => setWhy(!why)}>
              Why {mode === 'today' ? 'today' : 'this day'}?
            </button>
          )}
        </div>
        {why && (
          <ul className="why">
            {s.reasons.map((r, i) => {
              const text = explain(r);
              return text ? <li key={i}>{text}</li> : null;
            })}
            {!s.reasons.length && <li>Added by you.</li>}
          </ul>
        )}
      </div>

      {mode === 'today' && !finished && (
        <div className="session-actions">
          {startedAt ? (
            <>
              <span className="timer" aria-live="polite">
                {String(Math.floor(elapsed / 60)).padStart(2, '0')}:{String(elapsed % 60).padStart(2, '0')}
              </span>
              <button
                className="primary"
                onClick={() => {
                  const actual = Math.max(1, Math.round(elapsed / 60));
                  setStartedAt(null);
                  setSession(s.id, actual >= s.minutes * 0.6 ? { status: 'done', actualMinutes: actual } : { status: 'partial', actualMinutes: actual });
                }}
              >
                Finish
              </button>
            </>
          ) : partial ? (
            <form
              className="inline"
              onSubmit={(e) => {
                e.preventDefault();
                setSession(s.id, { status: 'partial', actualMinutes: Number(mins) });
              }}
            >
              <label>
                Minutes done <input type="number" min={1} max={600} value={mins} onChange={(e) => setMins(e.target.value)} />
              </label>
              <button className="primary">Save</button>
              <button type="button" onClick={() => setPartial(false)}>
                Cancel
              </button>
            </form>
          ) : (
            <>
              <button onClick={() => setStartedAt(Date.now())}>▶ Start</button>
              <button className="primary" onClick={() => setSession(s.id, { status: 'done' })}>
                ✓ Done
              </button>
              <button onClick={() => setPartial(true)}>Partly</button>
              <button className="ghost" onClick={() => setSession(s.id, { status: 'skipped' })}>
                Skip
              </button>
            </>
          )}
        </div>
      )}
      {mode === 'today' && finished && (
        <div className="session-actions">
          {s.status === 'done' && s.actualMinutes == null && <LogTime s={s} />}
          <button className="ghost" aria-label={`Undo check-in for ${sessionWhat(st, s)}`} onClick={() => setSession(s.id, { status: 'planned' })}>
            Undo
          </button>
        </div>
      )}
      {mode === 'edit' && s.status === 'planned' && <SessionEditor s={s} />}
    </article>
  );
}

function LogTime({ s }: { s: Session }) {
  const [v, setV] = useState(String(s.minutes));
  return (
    <form
      className="inline"
      onSubmit={(e) => {
        e.preventDefault();
        setSession(s.id, { actualMinutes: Number(v) });
      }}
      title="Logging real time teaches the planner your pace in this subject"
    >
      <label>
        Took <input type="number" min={1} max={600} value={v} onChange={(e) => setV(e.target.value)} /> min
      </label>
      <button>Log</button>
    </form>
  );
}

function SessionEditor({ s }: { s: Session }) {
  const [minutes, setMinutes] = useState(String(s.minutes));
  const [date, setDate] = useState(s.date);
  useEffect(() => {
    setMinutes(String(s.minutes));
    setDate(s.date);
  }, [s.minutes, s.date]);
  return (
    <div className="session-actions editor">
      <label>
        <span className="sr-only">Minutes</span>
        <input
          type="number"
          min={5}
          max={600}
          step={5}
          value={minutes}
          onChange={(e) => setMinutes(e.target.value)}
          onBlur={() => Number(minutes) !== s.minutes && setSession(s.id, { minutes: Number(minutes) })}
          aria-label="Minutes"
        />{' '}
        min
      </label>
      <input
        type="date"
        value={date}
        aria-label="Move to date"
        onChange={(e) => {
          setDate(e.target.value);
          if (e.target.value) setSession(s.id, { date: e.target.value });
        }}
      />
      <button className="ghost" title={s.pinned ? 'Let the planner move this again' : 'Keep this session where it is'} onClick={() => setSession(s.id, { pinned: !s.pinned })}>
        {s.pinned ? 'Unpin' : 'Pin'}
      </button>
      <button className="ghost danger" aria-label="Remove session" title="Remove (its time goes back to the plan)" onClick={() => act(api('DELETE', `/api/sessions/${s.id}`))}>
        ✕
      </button>
    </div>
  );
}

export function Readiness({ h }: { h: DeadlineHealth }) {
  const total = Math.max(h.totalMin, 1);
  const done = Math.min(100, (h.doneMin / total) * 100);
  const planned = Math.min(100 - done, (h.plannedMin / total) * 100);
  return (
    <div className="readiness" role="img" aria-label={`${Math.round(done)}% done, ${Math.round(planned)}% planned${h.shortfallMin ? `, ${h.shortfallMin} minutes don't fit` : ''}`}>
      <span className="done" style={{ width: `${done}%` }} />
      <span className="planned" style={{ width: `${planned}%` }} />
      {h.shortfallMin > 0 && <span className="short" style={{ width: `${100 - done - planned}%` }} />}
    </div>
  );
}

export function DeadlineRunway({ st, h, onSize }: { st: AppState; h: DeadlineHealth; onSize?: () => void }) {
  const d = st.deadlines.find((x) => x.id === h.deadlineId);
  if (!d) return null;
  const days = diffDays(st.today, d.dueAt);
  const state = h.needsMaterial ? 'unsized' : h.shortfallMin > 0 ? 'risk' : h.bufferShrunk ? 'tight' : 'ok';
  return (
    <li className={`runway ${state}`}>
      <div className="runway-head">
        <CourseTag st={st} id={d.courseId} />
        <span className="runway-title">{d.title}</span>
        <span className="chip">{d.kind}</span>
        <span className="runway-days">{days === 0 ? 'today' : days === 1 ? 'tomorrow' : `in ${days} days`}</span>
      </div>
      {h.needsMaterial ? (
        <div className="runway-meta">
          <strong className="warn-text">Not planned yet:</strong> add what's on it (chapters, pages, slides) so study time can be worked out.{' '}
          {onSize && (
            <button className="link" onClick={onSize}>
              Add material
            </button>
          )}
        </div>
      ) : (
        <>
          <Readiness h={h} />
          <div className="runway-meta">
            {fmtMin(h.doneMin)} done · {fmtMin(h.plannedMin)} planned · {fmtMin(h.totalMin)} needed
            {state === 'risk' && <strong className="risk-text"> · {fmtMin(h.shortfallMin)} doesn't fit</strong>}
            {state === 'tight' && <span className="tight-text"> · tight: buffer day used for study</span>}
          </div>
          {h.fixes.length > 0 && (
            <ul className="fixes">
              {h.fixes.map((f) => (
                <li key={f}>{f}</li>
              ))}
            </ul>
          )}
        </>
      )}
    </li>
  );
}

export function Card({ title, children, actions, className = '' }: { title?: ReactNode; children: ReactNode; actions?: ReactNode; className?: string }) {
  return (
    <section className={`card ${className}`}>
      {(title || actions) && (
        <header className="card-head">
          {title && <h2>{title}</h2>}
          {actions && <div className="card-actions">{actions}</div>}
        </header>
      )}
      {children}
    </section>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function ConfirmButton({ onConfirm, children, className = '', confirmText = 'Sure?' }: { onConfirm: () => void; children: ReactNode; className?: string; confirmText?: string }) {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(false), 4000);
    return () => clearTimeout(t);
  }, [armed]);
  return (
    <button
      className={`${className} ${armed ? 'armed' : ''}`}
      onClick={() => {
        if (armed) {
          setArmed(false);
          onConfirm();
        } else setArmed(true);
      }}
    >
      {armed ? confirmText : children}
    </button>
  );
}

/** Accessible modal dialog built on <dialog>. */
export function Modal({ open, onClose, title, children, wide }: { open: boolean; onClose: () => void; title: string; children: ReactNode; wide?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);
  return (
    <dialog ref={ref} className={`modal ${wide ? 'wide' : ''}`} onClose={onClose} onCancel={onClose} aria-label={title}>
      <header className="modal-head">
        <h2>{title}</h2>
        <button className="ghost" aria-label="Close" onClick={onClose}>
          ✕
        </button>
      </header>
      <div className="modal-body">{open && children}</div>
    </dialog>
  );
}

export function Segmented<T extends string | number>({ value, options, onChange, label }: { value: T; options: { value: T; label: string; title?: string }[]; onChange: (v: T) => void; label: string }) {
  return (
    <div className="segmented small" role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <button key={String(o.value)} role="radio" aria-checked={o.value === value} className={o.value === value ? 'active' : ''} title={o.title} onClick={() => onChange(o.value)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

export const TIME_STEPS = [0.5, 0.75, 1, 1.25, 1.5, 2];
export const timeLabel = (f: number) => (f === 1 ? 'Normal time' : f > 1 ? `+${Math.round((f - 1) * 100)}% time` : `−${Math.round((1 - f) * 100)}% time`);

/** "Less ← Normal → More" time stepper. */
export function TimeStepper({ value, onChange, label }: { value: number; onChange: (v: number) => void; label: string }) {
  const i = Math.max(0, TIME_STEPS.findIndex((s) => Math.abs(s - value) < 0.01));
  const idx = TIME_STEPS.some((s) => Math.abs(s - value) < 0.01) ? i : 2;
  return (
    <span className="stepper" role="group" aria-label={label}>
      <button className="ghost small" aria-label="Less time" disabled={idx === 0} onClick={() => onChange(TIME_STEPS[idx - 1])}>
        −
      </button>
      <span className={`stepper-value ${value > 1 ? 'more' : value < 1 ? 'less' : ''}`}>{timeLabel(TIME_STEPS[idx])}</span>
      <button className="ghost small" aria-label="More time" disabled={idx === TIME_STEPS.length - 1} onClick={() => onChange(TIME_STEPS[idx + 1])}>
        +
      </button>
    </span>
  );
}

/** Grade entry that matches the scale: number, letter/class list, or points out of a maximum. */
export function GradeInput({ scaleId, value, onChange }: { scaleId: string; value?: GradeValue; onChange: (g: GradeValue | undefined) => void }) {
  const scale = scaleById(scaleId);
  if (scale.kind === 'options') {
    return (
      <select value={value?.scale === scale.id ? String(value.value) : ''} onChange={(e) => onChange(e.target.value ? { scale: scale.id, value: e.target.value } : undefined)} aria-label="Grade">
        <option value="">Choose…</option>
        {scale.options!.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    );
  }
  if (scale.kind === 'points') {
    const v = value?.scale === 'points' ? value : undefined;
    return (
      <span className="inline-inputs">
        <input type="number" min={0} step="any" aria-label="Points scored" value={v?.value ?? ''} onChange={(e) => onChange({ scale: 'points', value: e.target.value === '' ? '' : Number(e.target.value), max: v?.max ?? 100 })} />
        <span>out of</span>
        <input type="number" min={1} step="any" aria-label="Maximum points" value={v?.max ?? 100} onChange={(e) => onChange({ scale: 'points', value: v?.value ?? '', max: Number(e.target.value) })} />
      </span>
    );
  }
  return (
    <input
      type="number"
      min={scale.min}
      max={scale.max}
      step={scale.step}
      aria-label="Grade"
      placeholder={`${scale.min}–${scale.max}`}
      value={value?.scale === scale.id ? String(value.value) : ''}
      onChange={(e) => onChange(e.target.value === '' ? undefined : { scale: scale.id, value: Number(e.target.value) })}
    />
  );
}

export const relTime = (iso?: string) => {
  if (!iso) return 'never';
  const s = Math.round((Date.now() - Date.parse(iso)) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
};
