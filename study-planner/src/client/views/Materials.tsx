import { useState } from 'react';
import { diffDays, fmtDate, fmtMin } from '../../shared/dates';
import type { Deadline, Material, PublicState, UnitType } from '../../shared/types';
import { estimateMinutes } from '../../planner/estimate';
import { act, api } from '../api';
import { Card, ConfirmButton, Empty, Readiness } from '../components';

const UNITS: { id: UnitType; label: string }[] = [
  { id: 'pages', label: 'pages' },
  { id: 'slides', label: 'slides' },
  { id: 'problems', label: 'problems' },
  { id: 'videoMinutes', label: 'video min' },
  { id: 'topics', label: 'topics' },
  { id: 'minutes', label: 'minutes' },
];

const sourceName = (st: PublicState, id?: string) => st.sources.find((s) => s.id === id)?.name;

export function Materials({ st }: { st: PublicState }) {
  return (
    <div className="stack">
      <div className="page-head">
        <h1>Courses & material</h1>
      </div>
      <AddDeadline st={st} />
      {!st.courses.length && <Empty>No courses yet. Add an exam above, or connect an app in “Apps”.</Empty>}
      {st.courses.map((c) => (
        <CourseCard key={c.id} st={st} courseId={c.id} />
      ))}
    </div>
  );
}

function AddDeadline({ st }: { st: PublicState }) {
  const [courseId, setCourseId] = useState(st.courses[0]?.id ?? '__new');
  const [courseName, setCourseName] = useState('');
  const [kind, setKind] = useState<Deadline['kind']>('exam');
  const [title, setTitle] = useState('');
  const [dueAt, setDueAt] = useState('');
  const isNew = courseId === '__new' || !st.courses.length;
  return (
    <Card title="Add an exam or deadline">
      <form
        className="form-grid"
        onSubmit={async (e) => {
          e.preventDefault();
          const ok = await act(
            api('POST', '/api/deadlines', { ...(isNew ? { courseName } : { courseId }), kind, title, dueAt }),
            'Added. The plan now includes it.',
          );
          if (ok) {
            setTitle('');
            setDueAt('');
            setCourseName('');
            const created = ok.courses.find((c) => c.name.toLowerCase() === courseName.toLowerCase());
            if (created) setCourseId(created.id);
          }
        }}
      >
        <label>
          Course
          <select value={isNew ? '__new' : courseId} onChange={(e) => setCourseId(e.target.value)}>
            {st.courses.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
            <option value="__new">+ New course…</option>
          </select>
        </label>
        {isNew && (
          <label>
            New course name
            <input required value={courseName} onChange={(e) => setCourseName(e.target.value)} placeholder="e.g. Biology" />
          </label>
        )}
        <label>
          Type
          <select value={kind} onChange={(e) => setKind(e.target.value as Deadline['kind'])}>
            <option value="exam">Exam</option>
            <option value="quiz">Quiz / test</option>
            <option value="assignment">Assignment</option>
          </select>
        </label>
        <label className="grow">
          Title
          <input required value={title} onChange={(e) => setTitle(e.target.value)} placeholder="e.g. Midterm: chapters 1–5" />
        </label>
        <label>
          Date
          <input required type="date" min={st.today} value={dueAt} onChange={(e) => setDueAt(e.target.value)} />
        </label>
        <div className="form-actions">
          <button className="primary">Add</button>
        </div>
      </form>
    </Card>
  );
}

function CourseCard({ st, courseId }: { st: PublicState; courseId: string }) {
  const course = st.courses.find((c) => c.id === courseId)!;
  const deadlines = st.deadlines.filter((d) => d.courseId === courseId).sort((a, b) => a.dueAt.localeCompare(b.dueAt));
  const materials = st.materials.filter((m) => m.courseId === courseId).sort((a, b) => a.order - b.order);
  const selfPaced = materials.filter((m) => !m.deadlineId || !deadlines.some((d) => d.id === m.deadlineId));
  return (
    <Card
      title={
        <span className="course-title">
          <input
            type="color"
            className="swatch"
            value={course.color}
            aria-label={`Colour for ${course.name}`}
            onChange={(e) => void act(api('PUT', `/api/courses/${course.id}`, { color: e.target.value }))}
          />
          {course.name}
          {course.samples >= 3 && (
            <span className="chip" title="Learned from the time you logged">
              you study {course.calibrationFactor > 1 ? `${Math.round((course.calibrationFactor - 1) * 100)}% slower` : `${Math.round((1 - course.calibrationFactor) * 100)}% faster`} than the default
            </span>
          )}
        </span>
      }
      actions={
        <ConfirmButton className="ghost danger small" confirmText="Delete course and its items?" onConfirm={() => act(api('DELETE', `/api/courses/${course.id}`))}>
          Delete
        </ConfirmButton>
      }
    >
      {deadlines.map((d) => (
        <DeadlineBlock key={d.id} st={st} d={d} materials={materials.filter((m) => m.deadlineId === d.id)} />
      ))}
      {selfPaced.length > 0 ? (
        <div className="deadline-block">
          <div className="db-head">
            <strong>Self-paced material</strong>
            <span className="muted small">No due date; it fills free time over the next 4 weeks.</span>
          </div>
          <MaterialList st={st} materials={selfPaced} />
          <AddMaterial st={st} courseId={courseId} />
        </div>
      ) : (
        <div className="deadline-block">
          <AddMaterial st={st} courseId={courseId} label="material without a due date" />
        </div>
      )}
    </Card>
  );
}

function DeadlineBlock({ st, d, materials }: { st: PublicState; d: Deadline; materials: Material[] }) {
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(d.title);
  const [dueAt, setDueAt] = useState(d.dueAt);
  const [kind, setKind] = useState(d.kind);
  const h = st.planMeta.health.deadlines.find((x) => x.deadlineId === d.id);
  const days = diffDays(st.today, d.dueAt);
  const src = sourceName(st, d.origin?.sourceId);
  return (
    <div className={`deadline-block ${d.status === 'done' ? 'is-done' : ''}`}>
      {editing ? (
        <form
          className="form-grid"
          onSubmit={async (e) => {
            e.preventDefault();
            if (await act(api('PUT', `/api/deadlines/${d.id}`, { title, dueAt, kind }))) setEditing(false);
          }}
        >
          <label className="grow">
            Title <input value={title} onChange={(e) => setTitle(e.target.value)} />
          </label>
          <label>
            Type
            <select value={kind} onChange={(e) => setKind(e.target.value as Deadline['kind'])}>
              <option value="exam">Exam</option>
              <option value="quiz">Quiz</option>
              <option value="assignment">Assignment</option>
            </select>
          </label>
          <label>
            Date <input type="date" value={dueAt} onChange={(e) => setDueAt(e.target.value)} />
          </label>
          <div className="form-actions">
            <button className="primary">Save</button>
            <button type="button" className="ghost" onClick={() => setEditing(false)}>
              Cancel
            </button>
          </div>
        </form>
      ) : (
        <div className="db-head">
          <span className="chip">{d.kind}</span>
          <strong>{d.title}</strong>
          <span className="muted">
            {fmtDate(d.dueAt)} · {days < 0 ? 'past' : days === 0 ? 'today' : `in ${days} d`}
          </span>
          {src && (
            <span className="chip source" title={d.userEdited.length ? `You edited: ${d.userEdited.join(', ')}` : undefined}>
              from {src}
              {d.userEdited.length ? ' · edited' : ''}
            </span>
          )}
          <span className="db-actions">
            <button className="ghost small" onClick={() => setEditing(true)}>
              Edit
            </button>
            <button className="ghost small" onClick={() => act(api('PUT', `/api/deadlines/${d.id}`, { status: d.status === 'done' ? 'active' : 'done' }))}>
              {d.status === 'done' ? 'Reopen' : 'Mark done'}
            </button>
            <ConfirmButton className="ghost danger small" confirmText="Delete with its material?" onConfirm={() => act(api('DELETE', `/api/deadlines/${d.id}`))}>
              Delete
            </ConfirmButton>
          </span>
        </div>
      )}
      {h && d.status === 'active' && (
        <div className="db-readiness">
          <Readiness h={h} />
          <span className="small muted">
            {fmtMin(h.doneMin)} done of {fmtMin(h.totalMin)}
            {h.shortfallMin > 0 ? ` · ${fmtMin(h.shortfallMin)} doesn't fit` : ''}
          </span>
        </div>
      )}
      {!materials.length && d.status === 'active' && <p className="muted small">No material listed, so the plan reserves {d.kind === 'exam' ? '3h' : d.kind === 'quiz' ? '1h' : '1h 30m'} of general preparation. Add material for a better plan.</p>}
      <MaterialList st={st} materials={materials} />
      {d.status === 'active' && <AddMaterial st={st} courseId={d.courseId} deadlineId={d.id} />}
    </div>
  );
}

function MaterialList({ st, materials }: { st: PublicState; materials: Material[] }) {
  if (!materials.length) return null;
  return (
    <ul className="materials">
      {materials.map((m) => (
        <MaterialRow key={m.id} st={st} m={m} />
      ))}
    </ul>
  );
}

function MaterialRow({ st, m }: { st: PublicState; m: Material }) {
  const course = st.courses.find((c) => c.id === m.courseId);
  const est = estimateMinutes(m, course);
  const [override, setOverride] = useState(m.estimateOverride != null ? String(m.estimateOverride) : '');
  const [qty, setQty] = useState(String(m.quantity));
  const put = (body: Record<string, unknown>) => act(api('PUT', `/api/materials/${m.id}`, body));
  return (
    <li className={`material ${m.done ? 'is-done' : ''}`}>
      <label className="check">
        <input type="checkbox" checked={m.done} onChange={(e) => put({ done: e.target.checked })} />
        <span className="sr-only">Done</span>
      </label>
      <span className="m-title">
        {m.link ? (
          <a href={m.link} target="_blank" rel="noreferrer">
            {m.title}
          </a>
        ) : (
          m.title
        )}
        {m.origin && <span className="chip source">from {sourceName(st, m.origin.sourceId) ?? 'app'}</span>}
      </span>
      <span className="m-qty">
        <input type="number" min={0} value={qty} aria-label="Amount" onChange={(e) => setQty(e.target.value)} onBlur={() => Number(qty) !== m.quantity && put({ quantity: Number(qty) })} />
        <select value={m.unitType} aria-label="Unit" onChange={(e) => put({ unitType: e.target.value })}>
          {UNITS.map((u) => (
            <option key={u.id} value={u.id}>
              {u.label}
            </option>
          ))}
        </select>
      </span>
      <select className="m-diff" value={m.difficulty} aria-label="Difficulty" onChange={(e) => put({ difficulty: Number(e.target.value) })}>
        <option value={1}>Easy</option>
        <option value={2}>Normal</option>
        <option value={3}>Hard</option>
      </select>
      <span className="m-est" title="Estimated study time. Type your own to override.">
        ≈
        <input
          type="number"
          min={0}
          placeholder={String(est)}
          value={override}
          aria-label="Estimated minutes (override)"
          onChange={(e) => setOverride(e.target.value)}
          onBlur={() => put({ estimateOverride: override === '' ? null : Number(override) })}
        />
        min
      </span>
      <ConfirmButton className="ghost danger small" confirmText="Delete?" onConfirm={() => act(api('DELETE', `/api/materials/${m.id}`))}>
        ✕
      </ConfirmButton>
    </li>
  );
}

function AddMaterial({ st, courseId, deadlineId, label = 'material' }: { st: PublicState; courseId: string; deadlineId?: string; label?: string }) {
  const [mode, setMode] = useState<'closed' | 'one' | 'paste'>('closed');
  const [title, setTitle] = useState('');
  const [quantity, setQuantity] = useState('10');
  const [unitType, setUnitType] = useState<UnitType>('pages');
  const [difficulty, setDifficulty] = useState('2');
  const [lines, setLines] = useState('');
  const course = st.courses.find((c) => c.id === courseId);
  const preview = estimateMinutes({ id: '', courseId, title, unitType, quantity: Number(quantity) || 0, difficulty: Number(difficulty) as 1 | 2 | 3, order: 0, done: false, userEdited: [] }, course);
  if (mode === 'closed') {
    return (
      <div className="add-material-links">
        <button className="link small" onClick={() => setMode('one')}>
          + Add {label}
        </button>
        <button className="link small" onClick={() => setMode('paste')}>
          + Paste a list
        </button>
      </div>
    );
  }
  if (mode === 'paste') {
    return (
      <form
        className="paste"
        onSubmit={async (e) => {
          e.preventDefault();
          if (await act(api('POST', '/api/materials', { courseId, deadlineId, lines, difficulty: Number(difficulty) }), 'Material added')) {
            setLines('');
            setMode('closed');
          }
        }}
      >
        <label>
          One item per line. Amounts are detected, e.g. “Chapter 3 – 24 pages”, “Lecture 5: 40 slides”, “Problem sheet 12 problems”, “Revise notes 2h”.
          <textarea required rows={5} value={lines} onChange={(e) => setLines(e.target.value)} placeholder={'Chapter 1 – 18 pages\nChapter 2 – 22 pages\nLecture slides: 40 slides'} />
        </label>
        <div className="form-actions">
          <button className="primary">Add all</button>
          <button type="button" className="ghost" onClick={() => setMode('closed')}>
            Cancel
          </button>
        </div>
      </form>
    );
  }
  return (
    <form
      className="form-grid"
      onSubmit={async (e) => {
        e.preventDefault();
        if (await act(api('POST', '/api/materials', { courseId, deadlineId, title, quantity: Number(quantity), unitType, difficulty: Number(difficulty) }), 'Material added')) {
          setTitle('');
          setMode('closed');
        }
      }}
    >
      <label className="grow">
        What
        <input required value={title} onChange={(e) => setTitle(e.target.value)} placeholder="e.g. Chapter 4" />
      </label>
      <label>
        Amount
        <input type="number" min={0} value={quantity} onChange={(e) => setQuantity(e.target.value)} />
      </label>
      <label>
        Unit
        <select value={unitType} onChange={(e) => setUnitType(e.target.value as UnitType)}>
          {UNITS.map((u) => (
            <option key={u.id} value={u.id}>
              {u.label}
            </option>
          ))}
        </select>
      </label>
      <label>
        Difficulty
        <select value={difficulty} onChange={(e) => setDifficulty(e.target.value)}>
          <option value="1">Easy</option>
          <option value="2">Normal</option>
          <option value="3">Hard</option>
        </select>
      </label>
      <div className="form-actions">
        <span className="muted small">≈ {fmtMin(preview)}</span>
        <button className="primary">Add</button>
        <button type="button" className="ghost" onClick={() => setMode('closed')}>
          Cancel
        </button>
      </div>
    </form>
  );
}
