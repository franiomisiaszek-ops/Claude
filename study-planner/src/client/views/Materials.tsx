import { useMemo, useState } from 'react';
import { diffDays, fmtDate, fmtMin } from '../../shared/dates';
import { matchScope, parseScope, scopeIsEmpty } from '../../shared/scope';
import type { Deadline, Material, UnitType } from '../../shared/types';
import { estimateMinutes, estimateSteps } from '../../planner/estimate';
import { act, api, toast, type AppState } from '../api';
import { goTo } from '../App';
import { Card, ConfirmButton, Empty, Modal, Readiness, Segmented, TimeStepper } from '../components';

const UNITS: { id: UnitType; label: string }[] = [
  { id: 'pages', label: 'pages' },
  { id: 'slides', label: 'slides' },
  { id: 'problems', label: 'problems' },
  { id: 'videoMinutes', label: 'video min' },
  { id: 'topics', label: 'topics' },
  { id: 'minutes', label: 'minutes' },
];

const sourceName = (st: AppState, id?: string) => (id === 'syllabus' ? 'your syllabus' : st.sources.find((s) => s.id === id)?.name);

export function Materials({ st }: { st: AppState }) {
  return (
    <div className="stack">
      <div className="page-head">
        <h1>Courses & material</h1>
      </div>
      <p className="muted">Study time is always worked out from the material: how much there is, how hard it is for you, and your pace. Adjust anything.</p>
      <AddDeadline st={st} />
      {st.features.ai && <SyllabusImport st={st} />}
      {!st.courses.length && <Empty>No courses yet. Add an exam above, or connect an app in “Connected apps”.</Empty>}
      {st.courses.map((c) => (
        <CourseCard key={c.id} st={st} courseId={c.id} />
      ))}
    </div>
  );
}

function CoursePicker({ st, courseId, setCourseId, courseName, setCourseName }: { st: AppState; courseId: string; setCourseId: (v: string) => void; courseName: string; setCourseName: (v: string) => void }) {
  const isNew = courseId === '__new' || !st.courses.length;
  return (
    <>
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
    </>
  );
}

function AddDeadline({ st }: { st: AppState }) {
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
          const ok = await act(api('POST', '/api/deadlines', { ...(isNew ? { courseName } : { courseId }), kind, title, dueAt }), 'Added. Now add what it covers so it can be planned.');
          if (ok) {
            setTitle('');
            setDueAt('');
            const created = ok.courses.find((c) => c.name.toLowerCase() === courseName.toLowerCase());
            if (created) setCourseId(created.id);
            setCourseName('');
          }
        }}
      >
        <CoursePicker st={st} courseId={courseId} setCourseId={setCourseId} courseName={courseName} setCourseName={setCourseName} />
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

function SyllabusImport({ st }: { st: AppState }) {
  const [open, setOpen] = useState(false);
  const [courseId, setCourseId] = useState(st.courses[0]?.id ?? '__new');
  const [courseName, setCourseName] = useState('');
  const [text, setText] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [termStart, setTermStart] = useState('');
  const [busy, setBusy] = useState(false);
  const isNew = courseId === '__new' || !st.courses.length;
  if (!open) {
    return (
      <Card className="slim">
        <div className="row-between">
          <span>
            <strong>Have a syllabus?</strong> <span className="muted">Paste or upload it and we'll find the exams and deadlines for you to review.</span>
          </span>
          <button onClick={() => setOpen(true)}>Import a syllabus</button>
        </div>
      </Card>
    );
  }
  return (
    <Card title="Import a syllabus" actions={<button className="ghost" aria-label="Close" onClick={() => setOpen(false)}>✕</button>}>
      <form
        className="stack-sm"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          const q = new URLSearchParams({ ...(isNew ? { courseName } : { courseId }), ...(termStart ? { termStart } : {}) });
          const res = await act(
            file ? api<{ added: number; found: number }>('POST', `/api/ai/syllabus?${q}`, undefined, { raw: file, contentType: file.type || 'application/octet-stream' }) : api<{ added: number; found: number }>('POST', `/api/ai/syllabus?${q}`, undefined, { raw: text, contentType: 'text/plain' }),
          );
          setBusy(false);
          if (!res) return;
          toast(res.added ? `Found ${res.found} items. ${res.added} are waiting in your Inbox to review.` : `Found ${res.found} items; nothing new to add.`);
          if (res.added) goTo('inbox');
        }}
      >
        <div className="form-grid">
          <CoursePicker st={st} courseId={courseId} setCourseId={setCourseId} courseName={courseName} setCourseName={setCourseName} />
          <label>
            First day of term (optional)
            <input type="date" value={termStart} onChange={(e) => setTermStart(e.target.value)} />
          </label>
        </div>
        <label className="field">
          Paste the syllabus text
          <textarea rows={6} value={text} onChange={(e) => setText(e.target.value)} disabled={!!file} placeholder="Week 1 … Midterm exam: Oct 14 (chapters 1–5) …" />
        </label>
        <label className="field">
          …or upload it (PDF, Word, text)
          <input type="file" accept=".pdf,.docx,.txt,.md,application/pdf" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        </label>
        <p className="small muted">The text (with contact details removed) is analyzed by Claude. Everything found goes to your Inbox; nothing is added until you accept it. Uses 1 of your {st.ai.dailyLimit} daily AI analyses.</p>
        <div className="form-actions">
          <button className="primary" disabled={busy || (!file && text.trim().length < 40)}>
            {busy ? 'Reading…' : 'Find exams and deadlines'}
          </button>
        </div>
      </form>
    </Card>
  );
}

function CourseCard({ st, courseId }: { st: AppState; courseId: string }) {
  const course = st.courses.find((c) => c.id === courseId)!;
  const deadlines = st.deadlines.filter((d) => d.courseId === courseId).sort((a, b) => a.dueAt.localeCompare(b.dueAt));
  const active = deadlines.filter((d) => d.status === 'active' && d.dueAt >= st.today);
  const past = deadlines.filter((d) => !active.includes(d));
  const materials = st.materials.filter((m) => m.courseId === courseId).sort((a, b) => a.order - b.order);
  const selfPaced = materials.filter((m) => !m.deadlineId || !deadlines.some((d) => d.id === m.deadlineId));
  const [showPast, setShowPast] = useState(false);
  return (
    <Card
      title={
        <span className="course-title">
          <input type="color" className="swatch" value={course.color} aria-label={`Colour for ${course.name}`} onChange={(e) => void act(api('PUT', `/api/courses/${course.id}`, { color: e.target.value }))} />
          {course.name}
        </span>
      }
      actions={
        <ConfirmButton className="ghost danger small" confirmText="Delete course and its items?" onConfirm={() => act(api('DELETE', `/api/courses/${course.id}`))}>
          Delete
        </ConfirmButton>
      }
    >
      {st.learning[courseId]?.explanation && <p className="explain small">📈 {st.learning[courseId].explanation}</p>}
      {active.map((d) => (
        <DeadlineBlock key={d.id} st={st} d={d} materials={materials.filter((m) => m.deadlineId === d.id)} />
      ))}
      {!active.length && <p className="muted small">No upcoming exams or deadlines in this course.</p>}
      <div className="deadline-block">
        {selfPaced.length > 0 && (
          <div className="db-head">
            <strong>Self-paced material</strong>
            <span className="muted small">No due date; it fills free time over the next 4 weeks.</span>
          </div>
        )}
        <MaterialList st={st} materials={selfPaced} />
        <AddMaterial st={st} courseId={courseId} label={selfPaced.length ? 'material' : 'material without a due date'} />
      </div>
      {past.length > 0 && (
        <div className="past">
          <button className="link small" onClick={() => setShowPast(!showPast)}>
            {showPast ? 'Hide' : 'Show'} {past.length} past or finished
          </button>
          {showPast &&
            past.map((d) => (
              <div key={d.id} className="past-row small">
                <span className="chip">{d.kind}</span> {d.title} · {fmtDate(d.dueAt)}
                {st.results.find((r) => r.deadlineId === d.id) ? ' · result saved' : d.kind !== 'assignment' ? ' · no result yet' : ''}
              </div>
            ))}
        </div>
      )}
    </Card>
  );
}

function deadlineEstimate(st: AppState, d: Deadline, materials: Material[]) {
  const course = st.courses.find((c) => c.id === d.courseId);
  const examAdjust = Math.min(1, d.timeAdjust ?? 1);
  const coverage = materials.filter((m) => !m.done).reduce((a, m) => a + Math.round(estimateMinutes(m, course) * examAdjust), 0);
  const all = materials.reduce((a, m) => a + Math.round(estimateMinutes(m, course) * examAdjust), 0);
  const g = st.learning[d.courseId]?.factor ?? 1;
  const practice = Math.max(0, Math.min(all * 2, all * Math.max(1, d.timeAdjust ?? 1) * g - all));
  return { coverage, all, practice: Math.round(practice / 5) * 5, g };
}

function DeadlineBlock({ st, d, materials }: { st: AppState; d: Deadline; materials: Material[] }) {
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(d.title);
  const [dueAt, setDueAt] = useState(d.dueAt);
  const [kind, setKind] = useState(d.kind);
  const h = st.planMeta.health.deadlines.find((x) => x.deadlineId === d.id);
  const days = diffDays(st.today, d.dueAt);
  const src = sourceName(st, d.origin?.sourceId);
  const est = deadlineEstimate(st, d, materials);
  return (
    <div id={`deadline-${d.id}`} className={`deadline-block ${h?.needsMaterial ? 'unsized' : ''}`}>
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
            {fmtDate(d.dueAt)} · {days === 0 ? 'today' : `in ${days} d`}
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
            <button className="ghost small" onClick={() => act(api('PUT', `/api/deadlines/${d.id}`, { status: 'done' }))}>
              Done
            </button>
            <ConfirmButton className="ghost danger small" confirmText="Delete with its material?" onConfirm={() => act(api('DELETE', `/api/deadlines/${d.id}`))}>
              Delete
            </ConfirmButton>
          </span>
        </div>
      )}

      {h && !h.needsMaterial && (
        <div className="db-readiness">
          <Readiness h={h} />
          <span className="small muted">
            {fmtMin(h.doneMin)} done of {fmtMin(h.totalMin)}
            {h.shortfallMin > 0 ? ` · ${fmtMin(h.shortfallMin)} doesn't fit` : ''}
          </span>
        </div>
      )}

      {materials.length > 0 && (
        <div className="estimate-summary small">
          <span>
            Material: <strong>{fmtMin(est.all)}</strong>
            {est.practice > 0 && (
              <>
                {' '}
                + practice <strong>{fmtMin(est.practice)}</strong>
              </>
            )}
            {est.g !== 1 && <span className="muted"> (×{est.g.toFixed(2)} from your grades)</span>}
          </span>
          <span className="time-control">
            For this {d.kind}: <TimeStepper label={`More or less time for ${d.title}`} value={d.timeAdjust ?? 1} onChange={(v) => act(api('PUT', `/api/deadlines/${d.id}`, { timeAdjust: v }))} />
          </span>
        </div>
      )}

      {materials.length === 0 && <Sizer st={st} d={d} />}
      <MaterialList st={st} materials={materials} />
      <AddMaterial st={st} courseId={d.courseId} deadlineId={d.id} />
    </div>
  );
}

/** An exam with no material: never invent time. Offer scope from the description, material entry, or a time the user chooses. */
function Sizer({ st, d }: { st: AppState; d: Deadline }) {
  const description = String((d.origin?.fields as any)?.description ?? '');
  const courseMaterials = st.materials.filter((m) => m.courseId === d.courseId && m.deadlineId !== d.id);
  const scope = useMemo(() => (description ? parseScope(`${d.title} ${description}`) : parseScope(d.title)), [description, d.title]);
  const match = useMemo(() => (scopeIsEmpty(scope) ? null : matchScope(scope, courseMaterials)), [scope, courseMaterials]);
  const [hours, setHours] = useState(d.roughMinutes ? String(d.roughMinutes / 60) : '');
  const [aiBusy, setAiBusy] = useState(false);
  const history = st.results
    .filter((r) => r.courseId === d.courseId && r.kind === d.kind && r.minutesInApp + r.minutesOutside > 0)
    .sort((a, b) => b.dueAt.localeCompare(a.dueAt))[0];

  const applyMatch = async (m: { matched: string[]; missing: { title: string; kind: string; quantity?: number }[] }) => {
    if (m.matched.length) await act(api('POST', `/api/deadlines/${d.id}/link`, { materialIds: m.matched }));
    if (m.missing.length)
      await act(
        api('POST', '/api/materials', {
          courseId: d.courseId,
          deadlineId: d.id,
          items: m.missing.map((x) => ({ title: x.title, unitType: x.kind === 'pages' ? 'pages' : 'topics', quantity: x.quantity ?? 1 })),
        }),
      );
    toast('Added. Set the real size of each (pages, slides…) for an accurate plan.');
  };

  return (
    <div className="sizer">
      <p>
        <strong>{d.roughMinutes ? `Using your time: ${fmtMin(d.roughMinutes)}.` : 'Not planned yet.'}</strong>{' '}
        <span className="muted">
          {d.roughMinutes
            ? 'Add the material for a plan based on what is actually on it.'
            : "Study time comes from the material, so add what's on it: chapters with page counts, slides, problem sets, lectures."}
        </span>
      </p>
      {match && (match.matched.length > 0 || match.missing.length > 0) && (
        <div className="scope-suggest">
          <span className="small">
            {description ? 'The description says' : 'The title says'}: <em>“{(description || d.title).slice(0, 140)}”</em>
          </span>
          <button className="small" onClick={() => applyMatch(match)}>
            Add {[...match.matched.map((id) => courseMaterials.find((m) => m.id === id)?.title), ...match.missing.map((x) => x.title)].filter(Boolean).slice(0, 4).join(', ')}
            {match.matched.length + match.missing.length > 4 ? '…' : ''}
          </button>
        </div>
      )}
      {!match && description && st.features.ai && (
        <div className="scope-suggest">
          <span className="small muted">There's a description from {sourceName(st, d.origin?.sourceId)}. Let AI find what's on it?</span>
          <button
            className="small"
            disabled={aiBusy}
            onClick={async () => {
              setAiBusy(true);
              const r = await act(api<{ matched: string[]; missing: { title: string; kind: string }[] }>('POST', `/api/ai/scope/${d.id}`, {}));
              setAiBusy(false);
              if (r && (r.matched.length || r.missing.length)) await applyMatch(r);
              else if (r) toast("The description doesn't say what's on it. Add the material yourself.");
            }}
          >
            {aiBusy ? 'Reading…' : 'Find the material'}
          </button>
        </div>
      )}
      <form
        className="inline"
        onSubmit={(e) => {
          e.preventDefault();
          void act(api('PUT', `/api/deadlines/${d.id}`, { roughMinutes: hours === '' ? null : Math.round(Number(hours) * 60), roughSource: 'user' }), hours === '' ? 'Cleared' : 'Planned with your time');
        }}
      >
        <label className="small">
          Don't know the material yet? Plan
          <input type="number" min={0.25} max={100} step={0.25} value={hours} onChange={(e) => setHours(e.target.value)} aria-label="Hours to plan" /> hours
        </label>
        <button className="small">{d.roughMinutes ? 'Update' : 'Use this'}</button>
        {history && !d.roughMinutes && (
          <button
            type="button"
            className="link small"
            onClick={() => act(api('PUT', `/api/deadlines/${d.id}`, { roughMinutes: history.minutesInApp + history.minutesOutside, roughSource: 'history' }), 'Planned like last time')}
          >
            Same as {history.title} ({fmtMin(history.minutesInApp + history.minutesOutside)})
          </button>
        )}
      </form>
    </div>
  );
}

function MaterialList({ st, materials }: { st: AppState; materials: Material[] }) {
  if (!materials.length) return null;
  return (
    <ul className="materials">
      {materials.map((m) => (
        <MaterialRow key={m.id} st={st} m={m} />
      ))}
    </ul>
  );
}

function MaterialRow({ st, m }: { st: AppState; m: Material }) {
  const course = st.courses.find((c) => c.id === m.courseId);
  const est = estimateMinutes(m, course);
  const [override, setOverride] = useState(m.estimateOverride != null ? String(m.estimateOverride) : '');
  const [qty, setQty] = useState(String(m.quantity));
  const [chain, setChain] = useState(false);
  const put = (body: Record<string, unknown>) => act(api('PUT', `/api/materials/${m.id}`, body));
  return (
    <li className={`material ${m.done ? 'is-done' : ''}`}>
      <div className="m-line">
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
          {m.weak && <span className="chip warn">weak topic</span>}
          {m.origin && <span className="chip source">from {sourceName(st, m.origin.sourceId) ?? 'app'}</span>}
          {m.note && <span className="muted small">{m.note}</span>}
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
        <ConfirmButton className="ghost danger small" confirmText="Delete?" onConfirm={() => act(api('DELETE', `/api/materials/${m.id}`))}>
          ✕
        </ConfirmButton>
      </div>
      <div className="m-controls">
        <span className="control">
          <span className="small muted">For me</span>
          <Segmented
            label={`How hard ${m.title} is for you`}
            value={m.difficulty}
            onChange={(v) => put({ difficulty: v })}
            options={[
              { value: 1, label: 'Easy', title: 'Easy for you: 20% less time' },
              { value: 2, label: 'Normal' },
              { value: 3, label: 'Hard', title: 'Hard for you: 40% more time' },
            ]}
          />
        </span>
        <span className="control">
          <TimeStepper label={`More or less time for ${m.title}`} value={m.timeAdjust ?? 1} onChange={(v) => put({ timeAdjust: v })} />
        </span>
        <span className="control m-est">
          <button className="link small" aria-expanded={chain} onClick={() => setChain(!chain)}>
            ≈ {fmtMin(est)}
          </button>
          <input
            type="number"
            min={0}
            placeholder="exact min"
            value={override}
            aria-label="Exact minutes (override)"
            onChange={(e) => setOverride(e.target.value)}
            onBlur={() => put({ estimateOverride: override === '' ? null : Number(override) })}
          />
        </span>
      </div>
      {chain && (
        <ol className="chain small">
          {estimateSteps(m, course).map((step, i) => (
            <li key={i}>
              {step.label}
              {step.factor ? ` ×${step.factor}` : ''} → <strong>{fmtMin(step.minutes)}</strong>
            </li>
          ))}
        </ol>
      )}
    </li>
  );
}

function AddMaterial({ st, courseId, deadlineId, label = 'material' }: { st: AppState; courseId: string; deadlineId?: string; label?: string }) {
  const [mode, setMode] = useState<'closed' | 'one' | 'paste' | 'upload'>('closed');
  const [title, setTitle] = useState('');
  const [quantity, setQuantity] = useState('10');
  const [unitType, setUnitType] = useState<UnitType>('pages');
  const [difficulty, setDifficulty] = useState('2');
  const [lines, setLines] = useState('');
  const course = st.courses.find((c) => c.id === courseId);
  const preview = estimateMinutes({ id: '', courseId, title, unitType, quantity: Number(quantity) || 0, difficulty: Number(difficulty) as 1 | 2 | 3, timeAdjust: 1, order: 0, done: false, userEdited: [] }, course);
  if (mode === 'closed') {
    return (
      <div className="add-material-links">
        <button className="link small" onClick={() => setMode('one')}>
          + Add {label}
        </button>
        <button className="link small" onClick={() => setMode('paste')}>
          + Paste a list
        </button>
        <button className="link small" onClick={() => setMode('upload')}>
          + Upload a file{st.features.ai ? ' (AI finds the chapters)' : ''}
        </button>
      </div>
    );
  }
  if (mode === 'upload') return <UploadDialog st={st} courseId={courseId} deadlineId={deadlineId} onClose={() => setMode('closed')} />;
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
        For me
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

interface Analysis {
  fileName: string;
  doc: { kind: string; units: number; unitType: 'pages' | 'slides'; exactCount: boolean; scanned: boolean; words: number };
  method: 'ai' | 'outline' | 'whole';
  items: { title: string; unitType: UnitType; quantity: number; startPage?: number; endPage?: number; difficulty: 1 | 2 | 3; confidence: string; evidence?: string }[];
  note?: string;
}

/** Upload a document → review the proposed units → add them. */
function UploadDialog({ st, courseId, deadlineId, onClose }: { st: AppState; courseId: string; deadlineId?: string; onClose: () => void }) {
  const [busy, setBusy] = useState(false);
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [rows, setRows] = useState<(Analysis['items'][number] & { include: boolean })[]>([]);
  const upload = async (file: File) => {
    if (file.size > 25 * 1024 * 1024) return toast('That file is over 25 MB.', 'error');
    setBusy(true);
    const res = await act(api<Analysis>('POST', `/api/ai/material?${new URLSearchParams({ courseId, fileName: file.name })}`, undefined, { raw: file, contentType: file.type || 'application/octet-stream' }));
    setBusy(false);
    if (!res) return;
    setAnalysis(res);
    setRows(res.items.map((i) => ({ ...i, include: true })));
  };
  const accept = async () => {
    const items = rows.filter((r) => r.include);
    if (!items.length) return onClose();
    if (await act(api('POST', '/api/materials', { courseId, deadlineId, items, fileName: analysis?.fileName }), `Added ${items.length} item${items.length > 1 ? 's' : ''}`)) onClose();
  };
  return (
    <Modal open onClose={onClose} title="Add material from a file" wide>
      {!analysis ? (
        <div className="stack-sm">
          <p className="muted">
            Upload a textbook chapter, lecture slides or notes (PDF, PowerPoint, Word or text, up to 25 MB). Pages and slides are counted exactly
            {st.features.ai ? '; Claude suggests where chapters start and how dense they are' : ''}. You review everything before it's added. The file is not stored.
          </p>
          <input type="file" accept=".pdf,.pptx,.docx,.txt,.md,application/pdf" disabled={busy} onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])} aria-label="Choose a file" />
          {busy && <p aria-live="polite">Reading the file…</p>}
          {st.features.ai && (
            <p className="small muted">
              AI analyses today: {st.ai.usedToday} of {st.ai.dailyLimit}.
            </p>
          )}
        </div>
      ) : (
        <div className="stack-sm">
          <p>
            <strong>{analysis.fileName}</strong>: {analysis.doc.units} {analysis.doc.unitType}
            {analysis.doc.exactCount ? '' : ' (estimated from length)'} ·{' '}
            {analysis.method === 'ai' ? 'units suggested by Claude' : analysis.method === 'outline' ? 'units from the document outline' : 'one unit'}
          </p>
          {analysis.note && <p className="small muted">{analysis.note}</p>}
          <table className="review">
            <thead>
              <tr>
                <th aria-label="Include" />
                <th>Unit</th>
                <th>Amount</th>
                <th>For me</th>
                <th>Time</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => {
                const update = (patch: Partial<typeof r>) => setRows(rows.map((x, j) => (j === i ? { ...x, ...patch } : x)));
                const minutes = estimateMinutes({ id: '', courseId, title: r.title, unitType: r.unitType, quantity: r.quantity, difficulty: r.difficulty, timeAdjust: 1, order: 0, done: false, userEdited: [] }, st.courses.find((c) => c.id === courseId));
                return (
                  <tr key={i} className={r.include ? '' : 'excluded'}>
                    <td>
                      <input type="checkbox" checked={r.include} onChange={(e) => update({ include: e.target.checked })} aria-label={`Include ${r.title}`} />
                    </td>
                    <td>
                      <input value={r.title} onChange={(e) => update({ title: e.target.value })} aria-label="Title" />
                      <div className="small muted">
                        {r.startPage ? `${r.unitType === 'slides' ? 'Slides' : 'Pages'} ${r.startPage}–${r.endPage}` : ''}
                        {r.confidence === 'low' ? ' · please check' : ''}
                      </div>
                    </td>
                    <td>
                      <input type="number" min={0} value={r.quantity} onChange={(e) => update({ quantity: Number(e.target.value) })} aria-label="Amount" /> {r.unitType}
                    </td>
                    <td>
                      <select value={r.difficulty} onChange={(e) => update({ difficulty: Number(e.target.value) as 1 | 2 | 3 })} aria-label="Difficulty for you">
                        <option value={1}>Easy</option>
                        <option value={2}>Normal</option>
                        <option value={3}>Hard</option>
                      </select>
                    </td>
                    <td className="small">{fmtMin(minutes)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <div className="form-actions">
            <button className="primary" onClick={accept}>
              Add {rows.filter((r) => r.include).length} item{rows.filter((r) => r.include).length === 1 ? '' : 's'}
            </button>
            <button className="ghost" onClick={onClose}>
              Cancel
            </button>
          </div>
        </div>
      )}
    </Modal>
  );
}
