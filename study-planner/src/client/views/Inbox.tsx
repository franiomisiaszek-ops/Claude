import { useState } from 'react';
import { fmtDate } from '../../shared/dates';
import type { InboxEntry, PublicState } from '../../shared/types';
import { act, api } from '../api';
import { Card, Empty } from '../components';

const resolve = (id: string, body: Record<string, unknown>) => act(api('POST', `/api/inbox/${id}`, body));

export function Inbox({ st }: { st: PublicState }) {
  const order: InboxEntry['change'][] = ['conflict', 'new', 'removed', 'changed'];
  const entries = [...st.inbox].sort((a, b) => order.indexOf(a.change) - order.indexOf(b.change) || a.createdAt.localeCompare(b.createdAt));
  return (
    <div className="stack">
      <div className="page-head">
        <h1>Inbox</h1>
      </div>
      <p className="muted">
        Clear items from your connected apps go straight into the plan. Anything uncertain, removed or in conflict with your own edits waits here for you.
      </p>
      {!entries.length && <Empty>All caught up. Nothing to review.</Empty>}
      {entries.map((e) => (
        <Entry key={e.id} st={st} e={e} />
      ))}
    </div>
  );
}

function Entry({ st, e }: { st: PublicState; e: InboxEntry }) {
  const source = st.sources.find((s) => s.id === e.sourceId)?.name ?? 'a connected app';
  const item = e.payload.item;
  const [kind, setKind] = useState<string>(item?.kind ?? 'assignment');
  const [dueAt, setDueAt] = useState<string>(item?.dueAt ?? '');
  if (e.change === 'new') {
    return (
      <Card className="inbox-entry">
        <div className="ie-head">
          <span className="chip">New?</span>
          <strong>{e.title}</strong>
          <span className="muted small">
            from {source} · {item.courseName}
          </span>
        </div>
        <p className="muted small">We weren't sure what this is. Tell us and it joins the plan.</p>
        <div className="form-grid">
          <label>
            It's a…
            <select value={kind} onChange={(x) => setKind(x.target.value)}>
              <option value="exam">Exam</option>
              <option value="quiz">Quiz / test</option>
              <option value="assignment">Assignment</option>
              <option value="material">Study material (no date)</option>
            </select>
          </label>
          {kind !== 'material' && (
            <label>
              Date <input type="date" value={dueAt} onChange={(x) => setDueAt(x.target.value)} />
            </label>
          )}
          <div className="form-actions">
            <button className="primary" onClick={() => resolve(e.id, { action: 'accept', overrides: { kind, dueAt: dueAt || undefined, confidence: 1 } })}>
              Add to plan
            </button>
            <button className="ghost" onClick={() => resolve(e.id, { action: 'ignore' })}>
              Ignore
            </button>
          </div>
        </div>
      </Card>
    );
  }
  if (e.change === 'conflict') {
    const fmt = (v: unknown) => (e.payload.field === 'dueAt' ? fmtDate(String(v)) : String(v));
    return (
      <Card className="inbox-entry conflict">
        <div className="ie-head">
          <span className="chip warn">Conflict</span>
          <strong>{e.title}</strong>
          <span className="muted small">from {source}</span>
        </div>
        <p>
          {source} changed the <strong>{e.payload.field === 'dueAt' ? 'date' : e.payload.field}</strong> to <strong>{fmt(e.payload.theirs)}</strong>, but you had set it to{' '}
          <strong>{fmt(e.payload.mine)}</strong>.
        </p>
        <div className="form-actions">
          <button className="primary" onClick={() => resolve(e.id, { action: 'theirs' })}>
            Use {fmt(e.payload.theirs)}
          </button>
          <button onClick={() => resolve(e.id, { action: 'mine' })}>Keep mine</button>
        </div>
      </Card>
    );
  }
  if (e.change === 'removed') {
    return (
      <Card className="inbox-entry">
        <div className="ie-head">
          <span className="chip">Removed</span>
          <strong>{e.title}</strong>
          <span className="muted small">from {source}</span>
        </div>
        <p className="muted">This item no longer appears in {source}. It may have been deleted or moved.</p>
        <div className="form-actions">
          <button className="primary" onClick={() => resolve(e.id, { action: 'remove' })}>
            Remove from plan
          </button>
          <button onClick={() => resolve(e.id, { action: 'keep' })}>Keep it (as my own item)</button>
        </div>
      </Card>
    );
  }
  return (
    <Card className="inbox-entry">
      <div className="ie-head">
        <span className="chip">Updated</span>
        <strong>{e.title}</strong>
        <span className="muted small">from {source}</span>
      </div>
      <p>
        Date moved from <strong>{fmtDate(e.payload.from)}</strong> to <strong>{fmtDate(e.payload.to)}</strong>. The plan has already adjusted.
      </p>
      <div className="form-actions">
        <button onClick={() => resolve(e.id, { action: 'ok' })}>Got it</button>
      </div>
    </Card>
  );
}
