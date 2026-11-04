import { useMemo, useState } from 'react';
import { addDays, fmtDate, fmtMin } from '../../shared/dates';
import { GRADE_SCALES, normalizeGrade, scaleById } from '../../shared/grades';
import type { GradeValue, TopicRating } from '../../shared/types';
import { act, api, toast, type AppState } from '../api';
import { GradeInput, Modal, Segmented } from '../components';

const OUTSIDE = [0, 30, 60, 120, 180];
const RATING_OPTIONS: { value: TopicRating; label: string }[] = [
  { value: 'weak', label: 'Weak' },
  { value: 'ok', label: 'OK' },
  { value: 'strong', label: 'Strong' },
  { value: 'not_on_exam', label: 'Not on it' },
];

/** After an exam: how it went, what was hard, and the grade (now or later). */
export function Debrief({ st, deadlineId, onClose }: { st: AppState; deadlineId: string; onClose: () => void }) {
  const d = st.deadlines.find((x) => x.id === deadlineId);
  const existing = st.results.find((r) => r.deadlineId === deadlineId);
  const course = st.courses.find((c) => c.id === d?.courseId);
  const materials = st.materials.filter((m) => m.deadlineId === deadlineId);
  const logged = useMemo(
    () =>
      st.sessions
        .filter((s) => s.deadlineId === deadlineId && (s.status === 'done' || s.status === 'partial'))
        .reduce((a, s) => a + (s.actualMinutes ?? (s.status === 'partial' ? Math.round(s.minutes / 2) : s.minutes)), 0),
    [st.sessions, deadlineId],
  );
  const [outside, setOutside] = useState(existing?.minutesOutside ?? 0);
  const [customOutside, setCustomOutside] = useState(!OUTSIDE.includes(existing?.minutesOutside ?? 0));
  const [felt, setFelt] = useState<number>(existing?.feltDifficulty ?? 0);
  const [ratings, setRatings] = useState<Record<string, TopicRating>>(existing?.topicRatings ?? {});
  const [scale, setScale] = useState(existing?.grade?.scale ?? course?.gradeScale ?? st.settings.gradeScale);
  const [grade, setGrade] = useState<GradeValue | undefined>(existing?.grade);
  const [later, setLater] = useState(!existing?.grade && !!existing);
  const [gradeDueAt, setGradeDueAt] = useState(existing?.gradeDueAt ?? addDays(st.today, 7));
  const [excluded, setExcluded] = useState(existing?.excluded ?? false);
  const [busy, setBusy] = useState(false);
  if (!d) return null;

  let gradeError: string | null = null;
  if (grade && !later) {
    try {
      normalizeGrade(grade);
    } catch (e) {
      gradeError = (e as Error).message;
    }
  }

  const save = async () => {
    setBusy(true);
    const body: Record<string, unknown> = { deadlineId, minutesOutside: outside, feltDifficulty: felt, topicRatings: ratings, excluded };
    if (!later && grade) body.grade = grade;
    if (later) body.gradeDueAt = gradeDueAt;
    const res = await act(api('POST', '/api/results', body));
    setBusy(false);
    if (!res) return;
    const l = res.learning[d.courseId];
    if (!later && grade && l?.explanation) toast(l.explanation);
    else toast(later ? 'Saved. We\'ll remind you to add the grade.' : 'Saved.');
    onClose();
  };

  return (
    <Modal open onClose={onClose} title={`How did ${d.title} go?`} wide>
      <div className="stack-sm">
        <p className="muted small">
          {course?.name} · {d.kind} on {fmtDate(d.dueAt)}. This takes 20 seconds and makes the next plan for this course fit you better.
        </p>

        {!materials.length && !d.roughMinutes && (
          <p className="small warn-text">No material was listed for this {d.kind}, so your grade is saved but can't be compared with study time. Add material to future exams so the planner can learn.</p>
        )}
        <section className="debrief-row">
          <h3>Study time</h3>
          <p>
            Logged in the app: <strong>{fmtMin(logged)}</strong>
          </p>
          <div className="chip-row" role="group" aria-label="Study outside the app">
            <span className="small muted">Also studied on your own:</span>
            {OUTSIDE.map((m) => (
              <button key={m} className={`chip-btn ${!customOutside && outside === m ? 'active' : ''}`} onClick={() => (setOutside(m), setCustomOutside(false))}>
                {m === 0 ? 'None' : `+${fmtMin(m)}`}
              </button>
            ))}
            <button className={`chip-btn ${customOutside ? 'active' : ''}`} onClick={() => setCustomOutside(true)}>
              Other
            </button>
            {customOutside && (
              <label className="inline small">
                <input type="number" min={0} max={6000} step={15} value={outside} onChange={(e) => setOutside(Number(e.target.value))} aria-label="Minutes studied outside the app" /> min
              </label>
            )}
          </div>
        </section>

        <section className="debrief-row">
          <h3>How did the exam feel?</h3>
          <Segmented
            label="How the exam felt"
            value={felt}
            onChange={setFelt}
            options={[
              { value: -1, label: 'Easier than expected' },
              { value: 0, label: 'As expected' },
              { value: 1, label: 'Harder than expected' },
            ]}
          />
        </section>

        {materials.length > 0 && (
          <section className="debrief-row">
            <h3>Topics</h3>
            <p className="small muted">Weak topics get more practice next time. "Not on it" leaves a topic out of the learning.</p>
            <ul className="topic-ratings">
              {materials.map((m) => (
                <li key={m.id}>
                  <span>{m.title}</span>
                  <Segmented label={`How ${m.title} went`} value={ratings[m.id] ?? ('' as TopicRating)} onChange={(v) => setRatings({ ...ratings, [m.id]: v })} options={RATING_OPTIONS} />
                </li>
              ))}
            </ul>
          </section>
        )}

        <section className="debrief-row">
          <h3>Grade</h3>
          <Segmented
            label="Grade known?"
            value={later ? 'later' : 'now'}
            onChange={(v) => setLater(v === 'later')}
            options={[
              { value: 'now', label: 'I have my grade' },
              { value: 'later', label: 'Grade comes later' },
            ]}
          />
          {later ? (
            <label className="inline small">
              Remind me on <input type="date" value={gradeDueAt} min={st.today} onChange={(e) => setGradeDueAt(e.target.value)} />
            </label>
          ) : (
            <div className="grade-entry">
              <select value={scale} aria-label="Grading scale" onChange={(e) => (setScale(e.target.value), setGrade(undefined))}>
                {GRADE_SCALES.map((g) => (
                  <option key={g.id} value={g.id}>
                    {g.label}
                  </option>
                ))}
              </select>
              <GradeInput scaleId={scale} value={grade} onChange={setGrade} />
              {gradeError && <span className="error-text small">{gradeError}</span>}
              {grade && !gradeError && <span className="small muted">≈ {Math.round(normalizeGrade(grade) * 100)}% of points ({scaleById(scale).label})</span>}
            </div>
          )}
        </section>

        <label className="check-row small">
          <input type="checkbox" checked={excluded} onChange={(e) => setExcluded(e.target.checked)} />
          Something unusual happened (sick, missed it, very unfair exam): don't learn from this one
        </label>

        <div className="form-actions">
          <button className="primary" disabled={busy || (!later && (!grade || !!gradeError))} onClick={save}>
            {busy ? 'Saving…' : 'Save'}
          </button>
          <button className="ghost" onClick={onClose}>
            Not now
          </button>
        </div>
      </div>
    </Modal>
  );
}
