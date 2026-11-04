import { describe, expect, it } from 'vitest';
import type { Source, SourceItem } from '../shared/types';
import { parseFeed } from './connectors/ics';
import { merge } from './merge';
import { emptyState } from './__tests__/fixtures';

const src = (type: Source['type'] = 'ics'): Source => ({ id: 'src1', type, name: 'Feed', config: {}, status: 'ok', intervalMin: 60, errorCount: 0 });
const exam = (over: Partial<SourceItem> = {}): SourceItem => ({ externalId: 'e1', kind: 'exam', confidence: 0.9, title: 'Bio midterm', courseName: 'Biology', dueAt: '2026-11-20', ...over });

describe('merge', () => {
  it('auto-accepts confident items and sends uncertain ones to the inbox', () => {
    const db = emptyState();
    merge(db, src(), { items: [exam(), exam({ externalId: 'x', title: 'Week 5', kind: 'assignment', confidence: 0.5 })], complete: true });
    expect(db.deadlines.map((d) => d.title)).toEqual(['Bio midterm']);
    expect(db.inbox.map((e) => [e.change, e.title])).toEqual([['new', 'Week 5']]);
    expect(db.courses.map((c) => c.name)).toEqual(['Biology']);
  });

  it('is a no-op when nothing changed', () => {
    const db = emptyState();
    merge(db, src(), { items: [exam()], complete: true });
    expect(merge(db, src(), { items: [exam()], complete: true })).toBe(false);
  });

  it('applies source changes to fields the user did not edit, and flags due-date moves', () => {
    const db = emptyState();
    merge(db, src(), { items: [exam()], complete: true });
    merge(db, src(), { items: [exam({ dueAt: '2026-11-22', title: 'Biology midterm' })], complete: true });
    expect(db.deadlines[0].dueAt).toBe('2026-11-22');
    expect(db.deadlines[0].title).toBe('Biology midterm');
    expect(db.inbox.some((e) => e.change === 'changed' && e.payload.to === '2026-11-22')).toBe(true);
  });

  it('turns a change to a user-edited field into a conflict instead of overwriting', () => {
    const db = emptyState();
    merge(db, src(), { items: [exam()], complete: true });
    db.deadlines[0].dueAt = '2026-11-19';
    db.deadlines[0].userEdited = ['dueAt'];
    merge(db, src(), { items: [exam({ dueAt: '2026-11-25' })], complete: true });
    expect(db.deadlines[0].dueAt).toBe('2026-11-19');
    const c = db.inbox.find((e) => e.change === 'conflict')!;
    expect(c.payload).toEqual({ field: 'dueAt', mine: '2026-11-19', theirs: '2026-11-25' });
  });

  it('proposes removal only after an item is missing twice from a windowed feed', () => {
    const db = emptyState();
    const range = { start: '2026-11-01', end: '2026-12-31' };
    merge(db, src(), { items: [exam()], complete: true, range });
    merge(db, src(), { items: [], complete: true, range });
    expect(db.inbox.filter((e) => e.change === 'removed')).toHaveLength(0);
    merge(db, src(), { items: [], complete: true, range });
    expect(db.inbox.filter((e) => e.change === 'removed')).toHaveLength(1);
    // Items outside the feed's window are never treated as removed.
    const db2 = emptyState();
    merge(db2, src(), { items: [exam({ dueAt: '2027-03-01' })], complete: true });
    for (let i = 0; i < 3; i++) merge(db2, src(), { items: [], complete: true, range });
    expect(db2.inbox).toHaveLength(0);
  });

  it('skips exact duplicates of existing deadlines and links materials to their deadline', () => {
    const db = emptyState();
    merge(db, src(), { items: [exam()], complete: true });
    merge(db, { ...src('google-calendar'), id: 'src2' }, { items: [exam({ externalId: 'cal1', title: 'Bio Midterm' })], complete: true });
    expect(db.deadlines).toHaveLength(1);
    merge(db, src(), { items: [exam(), { externalId: 'm1', kind: 'material', confidence: 0.9, title: 'Ch 1', courseName: 'Biology', deadlineExternalId: 'e1', unitType: 'pages', quantity: 10 }], complete: true });
    expect(db.materials[0].deadlineId).toBe(db.deadlines[0].id);
  });

  it('marks items done when the source reports them submitted', () => {
    const db = emptyState();
    merge(db, src('google-classroom'), { items: [exam({ kind: 'assignment' })], complete: true });
    merge(db, src('google-classroom'), { items: [exam({ kind: 'assignment', done: true })], complete: true });
    expect(db.deadlines[0].status).toBe('done');
  });
});

describe('ICS parsing', () => {
  const feed = [
    'BEGIN:VCALENDAR',
    'VERSION:2.0',
    'BEGIN:VEVENT',
    'UID:a1',
    'DTSTART:20261112T235900Z',
    'DTEND:20261112T235900Z',
    'SUMMARY:Lab report 3 [CHEM101]',
    'END:VEVENT',
    'BEGIN:VEVENT',
    'UID:a2',
    'DTSTART;VALUE=DATE:20261118',
    'SUMMARY:Midterm exam [BIO200]',
    'END:VEVENT',
    'BEGIN:VEVENT',
    'UID:a3',
    'DTSTART:20261105T170000Z',
    'DTEND:20261105T183000Z',
    'RRULE:FREQ=WEEKLY;COUNT=3',
    'SUMMARY:Football practice',
    'END:VEVENT',
    'END:VCALENDAR',
  ].join('\r\n');

  it('extracts deadlines with course codes from an LMS feed', () => {
    const { items } = parseFeed(feed, { tz: 'UTC', today: '2026-11-04', feedType: 'lms', fallbackCourse: 'Canvas' });
    const lab = items.find((i) => i.externalId === 'a1')!;
    expect(lab).toMatchObject({ kind: 'assignment', title: 'Lab report 3', courseName: 'CHEM101', dueAt: '2026-11-12' });
    expect(items.find((i) => i.externalId === 'a2')).toMatchObject({ kind: 'exam', courseName: 'BIO200', dueAt: '2026-11-18' });
  });

  it('turns ordinary events in a personal calendar feed into busy time, expanding recurrences', () => {
    const { items, busy } = parseFeed(feed, { tz: 'UTC', today: '2026-11-04', feedType: 'calendar', fallbackCourse: 'Cal' });
    expect(items.some((i) => i.title.startsWith('Football'))).toBe(false);
    expect(busy.filter((b) => b.title === 'Football practice').map((b) => b.date)).toEqual(['2026-11-05', '2026-11-12', '2026-11-19']);
    expect(busy[0]).toMatchObject({ startMin: 17 * 60, endMin: 18 * 60 + 30 });
  });
});
