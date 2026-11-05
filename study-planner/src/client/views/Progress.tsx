import { useState } from 'react';
import { addDays, fmtDate, fmtMin } from '../../shared/dates';
import { formatGrade, GRADE_SCALES, scaleById } from '../../shared/grades';
import type { GradeValue } from '../../shared/types';
import { courseFactor } from '../../planner/estimate';
import { act, api, type AppState } from '../api';
import { Card, ConfirmButton, CourseTag, Empty, GradeInput } from '../components';
import { Debrief } from './Debrief';

/** Exams and quizzes that are over and still need a debrief or a grade. */
export function pendingDebriefs(st: AppState) {
  const results = new Map(st.results.map((r) => [r.deadlineId, r]));
  return st.deadlines
    .filter((d) => d.kind !== 'assignment' && d.dueAt <= st.today && d.dueAt >= addDays(st.today, -21))
    .filter((d) => {
      const r = results.get(d.id);
      return !r || (r.score == null && !r.excluded && (!r.gradeDueAt || r.gradeDueAt <= st.today));
    })
    .sort((a, b) => b.dueAt.localeCompare(a.dueAt));
}

export function Progress({ st }: { st: AppState }) {
  const [open, setOpen] = useState<string | null>(null);
  const pending = pendingDebriefs(st);
  return (
    <div className="stack">
      <div className="page-head">
        <h1>Progress</h1>
      </div>
      <p className="muted">
        After each exam, tell the app how it went. It compares your grade with the time you studied and adjusts future plans for that course. It changes
        gradually, never cuts the time needed to cover your material, and always explains itself.
      </p>

      {pending.length > 0 && (
        <Card title="Waiting for your input">
          <ul className="pending-list">
            {pending.map((d) => (
              <li key={d.id}>
                <CourseTag st={st} id={d.courseId} />
                <strong>{d.title}</strong>
                <span className="muted small">{fmtDate(d.dueAt)}</span>
                <button className="primary small" onClick={() => setOpen(d.id)}>
                  {st.results.some((r) => r.deadlineId === d.id) ? 'Add grade' : 'How did it go?'}
                </button>
              </li>
            ))}
          </ul>
        </Card>
      )}

      {!st.courses.length && <Empty>No courses yet.</Empty>}
      {st.courses.map((c) => (
        <CourseProgress key={c.id} st={st} courseId={c.id} onOpen={setOpen} />
      ))}
      {open && <Debrief st={st} deadlineId={open} onClose={() => setOpen(null)} />}
    </div>
  );
}

function CourseProgress({ st, courseId, onOpen }: { st: AppState; courseId: string; onOpen: (id: string) => void }) {
  const c = st.courses.find((x) => x.id === courseId)!;
  const learned = st.learning[courseId];
  const results = st.results.filter((r) => r.courseId === courseId).sort((a, b) => b.dueAt.localeCompare(a.dueAt));
  const scaleId = c.gradeScale ?? st.settings.gradeScale;
  const [target, setTarget] = useState<GradeValue | undefined>(c.targetGrade ?? st.settings.targetGrade);
  const pace = courseFactor(c);
  return (
    <Card
      title={
        <span className="course-title">
          <span className="dot big" style={{ background: c.color }} aria-hidden />
          {c.name}
        </span>
      }
    >
      <div className="progress-grid">
        <div className="stat">
          <span className="stat-label">Time factor from grades</span>
          <span className="stat-value">{learned && learned.factor !== 1 ? `×${learned.factor.toFixed(2)}` : '×1.00'}</span>
          <span className="small muted">{learned?.locked ? 'Learning is off for this course' : learned?.n ? `from ${learned.n} graded exam${learned.n > 1 ? 's' : ''}` : 'starts after your first graded exam'}</span>
        </div>
        <div className="stat">
          <span className="stat-label">Your pace</span>
          <span className="stat-value">{pace === 1 ? 'Default' : `×${pace.toFixed(2)}`}</span>
          <span className="small muted">{c.samples >= 3 ? `from ${c.samples} timed sessions` : `${c.samples}/3 timed sessions logged`}</span>
        </div>
        <div className="stat">
          <span className="stat-label">Target grade</span>
          <span className="grade-entry">
            <GradeInput scaleId={scaleId} value={target} onChange={setTarget} />
            <button
              className="small"
              disabled={!target}
              onClick={() => act(api('PUT', `/api/courses/${c.id}`, { targetGrade: target, gradeScale: scaleId }), 'Target saved')}
            >
              Save
            </button>
          </span>
          <span className="small muted">Scale: {scaleById(scaleId).label}</span>
        </div>
      </div>
      {learned?.explanation && <p className="explain">{learned.explanation}</p>}
      {results.length > 0 && <EffortChart st={st} courseId={courseId} />}
      {results.length > 0 ? (
        <table className="results">
          <thead>
            <tr>
              <th>Exam</th>
              <th>Studied</th>
              <th>vs. material estimate</th>
              <th>Grade</th>
              <th aria-label="Actions" />
            </tr>
          </thead>
          <tbody>
            {results.map((r) => {
              const total = r.minutesInApp + r.minutesOutside;
              const obs = learned?.observations.find((o) => o.resultId === r.id);
              return (
                <tr key={r.id} className={r.excluded ? 'excluded' : ''}>
                  <td>
                    <strong>{r.title}</strong>
                    <div className="small muted">
                      {fmtDate(r.dueAt)}
                      {r.excluded && ' · not used for learning'}
                      {obs?.outlier && ' · unusual result, counts less'}
                    </div>
                  </td>
                  <td>{fmtMin(total)}</td>
                  <td>{r.baseEstimateMin ? `${Math.round((total / r.baseEstimateMin) * 100)}%` : <span className="small muted">no material listed</span>}</td>
                  <td>{r.grade ? formatGrade(r.grade) : <button className="link small" onClick={() => onOpen(r.deadlineId)}>Add grade</button>}</td>
                  <td className="row-actions">
                    <button className="ghost small" onClick={() => onOpen(r.deadlineId)}>
                      Edit
                    </button>
                    <button className="ghost small" onClick={() => act(api('PUT', `/api/results/${r.id}`, { excluded: !r.excluded }))}>
                      {r.excluded ? 'Use' : 'Ignore'}
                    </button>
                    <ConfirmButton className="ghost danger small" confirmText="Delete?" onConfirm={() => act(api('DELETE', `/api/results/${r.id}`))}>
                      ✕
                    </ConfirmButton>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      ) : (
        <p className="muted small">No exam results yet for this course.</p>
      )}
      <label className="check-row small">
        <input type="checkbox" checked={!!c.learningLocked} onChange={(e) => act(api('PUT', `/api/courses/${c.id}`, { learningLocked: e.target.checked }))} />
        Don't adjust this course from my grades
      </label>
    </Card>
  );
}

/** Effort (share of the material estimate) vs grade, with the target line. */
function EffortChart({ st, courseId }: { st: AppState; courseId: string }) {
  const learned = st.learning[courseId];
  const obs = learned?.observations ?? [];
  if (obs.length < 1) return null;
  const W = 320;
  const H = 150;
  const pad = 28;
  const maxX = Math.max(2, ...obs.map((o) => o.x)) * 1.1;
  const x = (v: number) => pad + (v / maxX) * (W - pad - 8);
  const y = (v: number) => H - pad - v * (H - pad - 8);
  // The plan's curve: S = 1 − exp(−x/τ), with τ = factor ÷ (−ln(1 − target)).
  const tau = learned.factor / -Math.log(1 - learned.target);
  const curve = Array.from({ length: 30 }, (_, i) => {
    const xv = (i / 29) * maxX;
    return `${x(xv)},${y(1 - Math.exp(-xv / tau))}`;
  }).join(' ');
  return (
    <figure className="effort-chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Your grades plotted against study time, with the learned curve and your target">
        <line x1={pad} y1={y(0)} x2={W - 8} y2={y(0)} className="axis" />
        <line x1={pad} y1={y(0)} x2={pad} y2={8} className="axis" />
        <line x1={pad} y1={y(learned.target)} x2={W - 8} y2={y(learned.target)} className="target" />
        <text x={W - 10} y={y(learned.target) - 4} textAnchor="end" className="label">
          target
        </text>
        <polyline points={curve} className="curve" />
        {obs.map((o) => (
          <circle key={o.resultId} cx={x(o.x)} cy={y(o.S)} r={5} className={o.outlier ? 'pt outlier' : 'pt'}>
            <title>{`${o.title}: ${Math.round(o.x * 100)}% of estimate, ${Math.round(o.S * 100)}%`}</title>
          </circle>
        ))}
        <text x={pad} y={H - 8} className="label">
          0
        </text>
        <text x={x(1)} y={H - 8} textAnchor="middle" className="label">
          1× estimate
        </text>
        <text x={4} y={12} className="label">
          100%
        </text>
      </svg>
      <figcaption className="small muted">Each dot is an exam. The curve is what the planner has learned about this course.</figcaption>
    </figure>
  );
}

export const GRADE_SCALE_OPTIONS = GRADE_SCALES;
