import { useState } from 'react';
import { fmtMin } from '../../shared/dates';
import type { PublicState } from '../../shared/types';
import { act, api } from '../api';
import { goTo } from '../App';
import { WeeklyEditor } from './Availability';

const PRESETS: { label: string; v: number[] }[] = [
  { label: 'Light', v: [0, 60, 60, 60, 60, 45, 90] },
  { label: 'Balanced', v: [0, 120, 120, 120, 120, 90, 150] },
  { label: 'Exam season', v: [120, 180, 180, 180, 180, 150, 240] },
];

export function Onboarding({ st }: { st: PublicState }) {
  const [step, setStep] = useState(0);
  const [weekly, setWeekly] = useState(st.availability.minutesByWeekday);
  const [digestTime, setDigestTime] = useState(st.settings.digestTime);
  const [busy, setBusy] = useState(false);

  const finish = async (source: 'demo' | 'apps' | 'manual') => {
    setBusy(true);
    await act(api('PUT', '/api/availability', { minutesByWeekday: weekly }));
    await act(api('PUT', '/api/settings', { digestTime }));
    if (source === 'demo') await act(api('POST', '/api/sources', { type: 'demo' }));
    await act(api('POST', '/api/onboarding'));
    if (source === 'apps') goTo('sources');
    if (source === 'manual') goTo('materials');
    setBusy(false);
  };

  return (
    <div className="onboarding">
      <div className="ob-card">
        <div className="ob-progress" aria-label={`Step ${step + 1} of 3`}>
          {[0, 1, 2].map((i) => (
            <span key={i} className={i <= step ? 'on' : ''} />
          ))}
        </div>
        {step === 0 && (
          <>
            <img src="/favicon.svg" alt="" width={56} height={56} />
            <h1>Your study plan, built for you</h1>
            <p>
              Connect Google Classroom, your school's calendar feed, or type in your exams. The planner works out <strong>what to study each day and for
              how long</strong>, keeps it updated when teachers post something new, and reminds you every morning, including when it's a day off.
            </p>
            <button className="primary big" onClick={() => setStep(1)}>
              Get started
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
            <h1>Where are your classes?</h1>
            <div className="ob-choices">
              <button disabled={busy} onClick={() => finish('apps')}>
                <strong>🔌 Connect apps</strong>
                <span>Google Classroom & Calendar, Canvas, Moodle, Blackboard, Brightspace, Outlook</span>
              </button>
              <button disabled={busy} onClick={() => finish('manual')}>
                <strong>✍️ I'll add exams myself</strong>
                <span>Type in exams, deadlines and what to read</span>
              </button>
              <button disabled={busy} onClick={() => finish('demo')}>
                <strong>🧪 Try with demo data</strong>
                <span>A sample term with four courses. Remove it any time.</span>
              </button>
            </div>
            <div className="ob-actions">
              <button className="ghost" onClick={() => setStep(1)}>
                Back
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
