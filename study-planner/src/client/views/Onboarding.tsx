import { useState } from 'react';
import { APP_NAME } from '../../shared/brand';
import { fmtMin } from '../../shared/dates';
import { GRADE_SCALES } from '../../shared/grades';
import type { GradeValue } from '../../shared/types';
import { act, api, type AppState } from '../api';
import { goTo } from '../App';
import { GradeInput } from '../components';
import { WeeklyEditor } from './Availability';

const PRESETS: { label: string; v: number[] }[] = [
  { label: 'Light', v: [0, 60, 60, 60, 60, 45, 90] },
  { label: 'Balanced', v: [0, 120, 120, 120, 120, 90, 150] },
  { label: 'Exam season', v: [120, 180, 180, 180, 180, 150, 240] },
];

export function Onboarding({ st }: { st: AppState }) {
  const [step, setStep] = useState(0);
  const [weekly, setWeekly] = useState(st.availability.minutesByWeekday);
  const [digestTime, setDigestTime] = useState(st.settings.digestTime);
  const [scale, setScale] = useState(st.settings.gradeScale);
  const [target, setTarget] = useState<GradeValue | undefined>(st.settings.targetGrade);
  const [busy, setBusy] = useState(false);
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;

  const finish = async (source: 'demo' | 'apps' | 'manual') => {
    setBusy(true);
    await act(api('PUT', '/api/availability', { minutesByWeekday: weekly }));
    await act(api('PUT', '/api/settings', { digestTime, timezone, gradeScale: scale, ...(target ? { targetGrade: target } : {}) }));
    if (source === 'demo') await act(api('POST', '/api/sources', { type: 'demo' }));
    await act(api('POST', '/api/onboarding', { timezone }));
    if (source === 'apps') goTo('sources');
    if (source === 'manual') goTo('materials');
    setBusy(false);
  };

  return (
    <div className="onboarding">
      <div className="ob-card">
        <div className="ob-progress" aria-label={`Step ${step + 1} of 4`}>
          {[0, 1, 2, 3].map((i) => (
            <span key={i} className={i <= step ? 'on' : ''} />
          ))}
        </div>
        {step === 0 && (
          <>
            <img src="/favicon.svg" alt="" width={56} height={56} />
            <h1>Welcome{st.user.name ? `, ${st.user.name.split(' ')[0]}` : ''}!</h1>
            <p>
              {APP_NAME} works out <strong>what to study each day and for how long</strong> from your real material: pages, slides, problems and lectures. It
              keeps up when teachers post something new, reminds you every morning, and learns from your grades.
            </p>
            <button className="primary big" onClick={() => setStep(1)}>
              Let's set it up
            </button>
          </>
        )}
        {step === 1 && (
          <>
            <h1>When can you study?</h1>
            <p className="muted">Roughly how much time per day. You can block specific dates later.</p>
            <div className="segmented">
              {PRESETS.map((p) => (
                <button key={p.label} className={weekly.join() === p.v.join() ? 'active' : ''} onClick={() => setWeekly(p.v)}>
                  {p.label}
                </button>
              ))}
            </div>
            <WeeklyEditor value={weekly} onChange={setWeekly} />
            <p className="muted small">{fmtMin(weekly.reduce((a, b) => a + b, 0))} per week</p>
            <label className="inline-label">
              Daily reminder at <input type="time" value={digestTime} onChange={(e) => setDigestTime(e.target.value)} />
            </label>
            <div className="ob-actions">
              <button className="ghost" onClick={() => setStep(0)}>
                Back
              </button>
              <button className="primary" onClick={() => setStep(2)}>
                Next
              </button>
            </div>
          </>
        )}
        {step === 2 && (
          <>
            <h1>How are you graded?</h1>
            <p className="muted">After each exam you can tell the app your grade, and it learns how much time you need. Pick your scale and the grade you usually aim for.</p>
            <label className="field">
              Grading scale
              <select value={scale} onChange={(e) => (setScale(e.target.value), setTarget(undefined))}>
                {GRADE_SCALES.map((g) => (
                  <option key={g.id} value={g.id}>
                    {g.label}
                    {g.hint ? ` (${g.hint})` : ''}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              Usual target
              <GradeInput scaleId={scale} value={target} onChange={setTarget} />
            </label>
            <div className="ob-actions">
              <button className="ghost" onClick={() => setStep(1)}>
                Back
              </button>
              <button className="primary" onClick={() => setStep(3)}>
                Next
              </button>
            </div>
          </>
        )}
        {step === 3 && (
          <>
            <h1>Where are your classes?</h1>
            <div className="ob-choices">
              <button disabled={busy} onClick={() => finish('apps')}>
                <strong>🔌 Connect apps</strong>
                <span>Google Classroom & Calendar, Canvas, Moodle, Blackboard, Brightspace, Outlook</span>
              </button>
              <button disabled={busy} onClick={() => finish('manual')}>
                <strong>✍️ I'll add exams myself</strong>
                <span>Type in exams and what's on them, or upload a syllabus or chapter</span>
              </button>
              <button disabled={busy} onClick={() => finish('demo')}>
                <strong>🧪 Try with demo data</strong>
                <span>A sample term with four courses. Remove it any time.</span>
              </button>
            </div>
            <div className="ob-actions">
              <button className="ghost" onClick={() => setStep(2)}>
                Back
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
