import { describe, expect, it } from 'vitest';
import { classify, parseMaterialLine, splitCourse } from './classify';

describe('classify', () => {
  it('detects exams and quizzes by keyword in several languages', () => {
    expect(classify({ title: 'Biology midterm', context: 'lms-feed', hasDue: true }).kind).toBe('exam');
    expect(classify({ title: 'Sprawdzian z historii', context: 'lms-feed', hasDue: true }).kind).toBe('exam');
    expect(classify({ title: 'Kartkówka – ułamki', context: 'lms-feed', hasDue: true }).kind).toBe('quiz');
    expect(classify({ title: 'Klausur Mathe', context: 'lms-feed', hasDue: true }).kind).toBe('exam');
  });
  it('does not match keywords inside other words', () => {
    expect(classify({ title: 'Testimony reading', context: 'calendar', hasDue: true }).kind).toBe('other');
    expect(classify({ title: 'Latest news', context: 'calendar', hasDue: true }).kind).toBe('other');
  });
  it('uses Classroom context', () => {
    expect(classify({ title: 'Read chapter 4', context: 'classroom-material', hasDue: false })).toEqual({ kind: 'material', confidence: 0.9 });
    expect(classify({ title: 'Worksheet 3', context: 'classroom-work', hasDue: true }).kind).toBe('assignment');
    expect(classify({ title: 'Unit test 2', context: 'classroom-work', hasDue: true }).kind).toBe('exam');
    expect(classify({ title: 'Optional reading', context: 'classroom-work', hasDue: false }).kind).toBe('material');
  });
  it('treats ordinary calendar events as non-study and exam-shaped events with high confidence', () => {
    expect(classify({ title: 'Dentist', context: 'calendar', hasDue: true, durationMin: 60 }).kind).toBe('other');
    const exam = classify({ title: 'Chemistry exam', context: 'calendar', hasDue: true, durationMin: 90 });
    expect(exam.kind).toBe('exam');
    expect(exam.confidence).toBeGreaterThanOrEqual(0.7);
    expect(classify({ title: 'Chemistry exam', context: 'calendar', hasDue: true, allDay: true }).confidence).toBeLessThan(0.7);
  });
  it('sends ambiguous LMS events to review', () => {
    expect(classify({ title: 'Week 5', context: 'lms-feed', hasDue: true, startMin: 600 }).confidence).toBeLessThan(0.7);
  });
});

describe('helpers', () => {
  it('splits Canvas course codes', () => {
    expect(splitCourse('HW3 [BIO101]')).toEqual({ title: 'HW3', course: 'BIO101' });
    expect(splitCourse('Essay')).toEqual({ title: 'Essay' });
  });
  it('parses pasted material lines', () => {
    expect(parseMaterialLine('Chapter 3 - 24 pages')).toEqual({ title: 'Chapter 3', unitType: 'pages', quantity: 24 });
    expect(parseMaterialLine('Lecture 5: 40 slides')).toEqual({ title: 'Lecture 5', unitType: 'slides', quantity: 40 });
    expect(parseMaterialLine('- Problem sheet, 12 problems')).toEqual({ title: 'Problem sheet', unitType: 'problems', quantity: 12 });
    expect(parseMaterialLine('Revise notes 2h')).toEqual({ title: 'Revise notes', unitType: 'minutes', quantity: 120 });
    expect(parseMaterialLine('Photosynthesis')).toEqual({ title: 'Photosynthesis', unitType: 'topics', quantity: 1 });
    expect(parseMaterialLine('pages 10-29 of the textbook')?.quantity).toBe(20);
    expect(parseMaterialLine('   ')).toBeNull();
    expect(parseMaterialLine('2. Cell membranes')).toEqual({ title: 'Cell membranes', unitType: 'topics', quantity: 1 });
  });
});
