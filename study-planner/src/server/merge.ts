/**
 * Merging imported items into a user's state. Pure functions on `UserState`; no I/O.
 * Shown value = the user's edit if they made one, otherwise the source's value.
 */
import type { Course, Deadline, InboxEntry, Material, Source, SourceItem, SyncResult, UserState } from '../shared/types';
import { hash } from '../planner/planner';
import { randomId } from './crypto';

type DB = UserState;
const uid = (prefix: string) => randomId(prefix, 9);

export const DEFAULT_INTERVAL: Record<Source['type'], number> = {
  demo: 60,
  ics: 180,
  'google-classroom': 30,
  'google-calendar': 15,
};

/** Items below this confidence wait in the inbox instead of entering the plan. */
export const AUTO_ACCEPT = 0.7;

const COLORS = ['#4f7cff', '#e0663a', '#2fa36b', '#b05cd6', '#d4a017', '#1aa3b8', '#d64f7c', '#7a8b3a'];

const normTitle = (s: string) => s.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').trim();

export function findOrCreateCourse(db: DB, name: string): Course {
  const clean = name.trim() || 'General';
  const found = db.courses.find((c) => c.name.toLowerCase() === clean.toLowerCase());
  if (found) return found;
  const course: Course = { id: uid('c_'), name: clean, color: COLORS[db.courses.length % COLORS.length], calibrationFactor: 1, samples: 0 };
  db.courses.push(course);
  return course;
}

const itemHash = (i: SourceItem) => hash(JSON.stringify([i.kind, i.title, i.courseName, i.dueAt, i.done, i.unitType, i.quantity, i.deadlineExternalId]));

const isDeadlineItem = (i: SourceItem) => i.kind !== 'material' && !!i.dueAt;

/** Creates a deadline or material from an imported item. */
export function createFromItem(db: DB, source: Pick<Source, 'id'>, item: SourceItem): Deadline | Material {
  const course = findOrCreateCourse(db, item.courseName);
  const origin = { sourceId: source.id, externalId: item.externalId, rawHash: itemHash(item), fields: { ...item } as Record<string, unknown>, missingCount: 0 };
  if (isDeadlineItem(item)) {
    const d: Deadline = {
      id: uid('d_'),
      courseId: course.id,
      kind: item.kind as Deadline['kind'],
      title: item.title,
      dueAt: item.dueAt!,
      status: item.done ? 'done' : 'active',
      origin,
      userEdited: [],
      confidence: item.confidence,
      timeAdjust: 1,
    };
    db.deadlines.push(d);
    return d;
  }
  const linked = item.deadlineExternalId
    ? db.deadlines.find((d) => d.origin?.sourceId === source.id && d.origin.externalId === item.deadlineExternalId)
    : undefined;
  const m: Material = {
    id: uid('m_'),
    courseId: course.id,
    deadlineId: linked?.id,
    title: item.title,
    unitType: item.unitType ?? 'topics',
    quantity: item.quantity ?? 1,
    difficulty: 2,
    timeAdjust: 1,
    order: db.materials.filter((x) => x.courseId === course.id).length,
    done: !!item.done,
    origin,
    userEdited: [],
    link: item.link,
  };
  db.materials.push(m);
  return m;
}

function addInbox(db: DB, e: Omit<InboxEntry, 'id' | 'createdAt' | 'resolved'>): void {
  const dup = db.inbox.find((x) => !x.resolved && x.sourceId === e.sourceId && x.externalId === e.externalId && x.change === e.change && (e.change !== 'conflict' || x.payload.field === e.payload.field));
  if (dup) {
    dup.payload = e.payload;
    dup.title = e.title;
    return;
  }
  db.inbox.push({ ...e, id: uid('i_'), createdAt: new Date().toISOString(), resolved: false });
}

/**
 * Merges a connector's result into the DB. Returns true when anything changed.
 * The value shown is the user's edit if they made one, otherwise the source's value.
 * A source change to a field the user edited becomes a conflict card in the inbox.
 */
export function merge(db: DB, source: Source, result: SyncResult): boolean {
  if (result.notModified) return false;
  let changed = false;
  const mine = <T extends Deadline | Material>(list: T[]) => new Map(list.filter((x) => x.origin?.sourceId === source.id).map((x) => [x.origin!.externalId, x]));
  const deadlines = mine(db.deadlines);
  const materials = mine(db.materials);
  const ignored = new Set(db.inbox.filter((e) => e.resolved && e.payload?.ignored && e.sourceId === source.id).map((e) => e.externalId));
  const seen = new Set(result.items.map((i) => i.externalId));

  const items = [...result.items].sort((a, b) => Number(isDeadlineItem(b)) - Number(isDeadlineItem(a)));
  for (const item of items) {
    const existing = deadlines.get(item.externalId) ?? materials.get(item.externalId);
    const h = itemHash(item);
    if (!existing) {
      if (ignored.has(item.externalId)) continue;
      // Exact duplicate already present from another source or typed in by hand.
      if (isDeadlineItem(item)) {
        const course = db.courses.find((c) => c.name.toLowerCase() === item.courseName.toLowerCase());
        const dup = db.deadlines.find((d) => d.courseId === course?.id && d.dueAt === item.dueAt && normTitle(d.title) === normTitle(item.title));
        if (dup) continue;
      }
      if (item.confidence >= AUTO_ACCEPT) {
        const created = createFromItem(db, source, item);
        if ('dueAt' in created) deadlines.set(item.externalId, created);
        else materials.set(item.externalId, created);
        db.inbox = db.inbox.filter((e) => !(e.sourceId === source.id && e.externalId === item.externalId && !e.resolved));
      } else {
        addInbox(db, { change: 'new', entity: isDeadlineItem(item) ? 'deadline' : 'material', sourceId: source.id, externalId: item.externalId, title: item.title, payload: { item } });
      }
      changed = true;
      continue;
    }
    const origin = existing.origin!;
    if (origin.missingCount) {
      origin.missingCount = 0;
      db.inbox = db.inbox.filter((e) => !(e.change === 'removed' && e.refId === existing.id && !e.resolved));
      changed = true;
    }
    if (origin.rawHash === h) continue;
    changed = true;
    const old = origin.fields as Partial<SourceItem>;
    if ('dueAt' in existing) {
      for (const field of ['title', 'dueAt', 'kind'] as const) {
        const theirs = item[field];
        if (theirs == null || theirs === old[field] || theirs === 'material') continue;
        if (existing.userEdited.includes(field)) {
          addInbox(db, { change: 'conflict', entity: 'deadline', refId: existing.id, sourceId: source.id, externalId: item.externalId, title: existing.title, payload: { field, mine: existing[field], theirs } });
        } else {
          if (field === 'dueAt') {
            addInbox(db, { change: 'changed', entity: 'deadline', refId: existing.id, sourceId: source.id, externalId: item.externalId, title: existing.title, payload: { field, from: existing.dueAt, to: theirs } });
          }
          (existing as any)[field] = theirs;
        }
      }
      if (item.done && existing.status === 'active') existing.status = 'done';
    } else {
      for (const field of ['title', 'quantity', 'unitType'] as const) {
        const theirs = item[field];
        if (theirs == null || theirs === old[field] || existing.userEdited.includes(field)) continue;
        (existing as any)[field] = theirs;
      }
      if (item.done && !existing.done) existing.done = true;
      if (item.deadlineExternalId && !existing.userEdited.includes('deadlineId')) {
        const d = deadlines.get(item.deadlineExternalId);
        if (d) existing.deadlineId = d.id;
      }
    }
    origin.rawHash = h;
    origin.fields = { ...item };
  }

  if (result.complete) {
    const threshold = source.type === 'ics' || source.type === 'google-calendar' ? 2 : 1;
    const inRange = (x: Deadline | Material) => {
      if (!result.range) return true;
      if (!('dueAt' in x)) return true;
      return x.dueAt >= result.range.start && x.dueAt <= result.range.end;
    };
    for (const x of [...deadlines.values(), ...materials.values()]) {
      if (seen.has(x.origin!.externalId) || !inRange(x)) continue;
      if ('dueAt' in x && x.status === 'done') continue;
      x.origin!.missingCount = (x.origin!.missingCount ?? 0) + 1;
      if (x.origin!.missingCount >= threshold) {
        addInbox(db, { change: 'removed', entity: 'dueAt' in x ? 'deadline' : 'material', refId: x.id, sourceId: source.id, externalId: x.origin!.externalId, title: x.title, payload: {} });
      }
      changed = true;
    }
    // Drop pending "new" proposals the source no longer has.
    const before = db.inbox.length;
    db.inbox = db.inbox.filter((e) => e.resolved || e.change !== 'new' || e.sourceId !== source.id || seen.has(e.externalId));
    if (db.inbox.length !== before) changed = true;
  }

  if (result.busy) {
    const next = result.busy.map((b) => ({ ...b, sourceId: source.id }));
    const prev = db.busy.filter((b) => b.sourceId === source.id);
    if (JSON.stringify(prev) !== JSON.stringify(next)) {
      db.busy = [...db.busy.filter((b) => b.sourceId !== source.id), ...next];
      changed = true;
    }
  }
  return changed;
}

