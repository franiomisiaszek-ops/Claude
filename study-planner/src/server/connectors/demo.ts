import { addDays } from '../../shared/dates';
import type { SourceItem } from '../../shared/types';
import type { Connector } from './types';

/**
 * Sample data so the app is fully usable without any accounts.
 * Dates are relative to the day the demo source was added, so they don't drift.
 */
export const syncDemo: Connector = async ({ source, today }) => {
  const a = source.config.anchor ?? today;
  const d = (n: number) => addDays(a, n);
  const items: SourceItem[] = [
    { externalId: 'bio-mid', kind: 'exam', confidence: 0.95, title: 'Biology midterm', courseName: 'Biology', dueAt: d(12) },
    { externalId: 'bio-ch4', kind: 'material', confidence: 0.9, title: 'Ch. 4 Cell structure', courseName: 'Biology', unitType: 'pages', quantity: 18, deadlineExternalId: 'bio-mid' },
    { externalId: 'bio-ch5', kind: 'material', confidence: 0.9, title: 'Ch. 5 Membranes and transport', courseName: 'Biology', unitType: 'pages', quantity: 22, deadlineExternalId: 'bio-mid' },
    { externalId: 'bio-ch6', kind: 'material', confidence: 0.9, title: 'Ch. 6 Metabolism', courseName: 'Biology', unitType: 'pages', quantity: 26, deadlineExternalId: 'bio-mid' },
    { externalId: 'bio-slides', kind: 'material', confidence: 0.9, title: 'Lecture slides: Enzymes', courseName: 'Biology', unitType: 'slides', quantity: 45, deadlineExternalId: 'bio-mid' },

    { externalId: 'calc-quiz', kind: 'quiz', confidence: 0.9, title: 'Calculus quiz: derivatives', courseName: 'Calculus', dueAt: d(6) },
    { externalId: 'calc-ps3', kind: 'material', confidence: 0.9, title: 'Problem set 3', courseName: 'Calculus', unitType: 'problems', quantity: 10, deadlineExternalId: 'calc-quiz' },
    { externalId: 'calc-vid', kind: 'material', confidence: 0.9, title: 'Lecture 7 recording', courseName: 'Calculus', unitType: 'videoMinutes', quantity: 50, deadlineExternalId: 'calc-quiz' },
    { externalId: 'calc-hw4', kind: 'assignment', confidence: 0.85, title: 'Calculus homework 4', courseName: 'Calculus', dueAt: d(9) },

    { externalId: 'hist-essay', kind: 'assignment', confidence: 0.9, title: 'History essay: Industrial Revolution', courseName: 'History', dueAt: d(16) },
    { externalId: 'hist-src', kind: 'material', confidence: 0.9, title: 'Primary sources reader', courseName: 'History', unitType: 'pages', quantity: 30, deadlineExternalId: 'hist-essay' },

    { externalId: 'chem-final', kind: 'exam', confidence: 0.95, title: 'Chemistry final exam', courseName: 'Chemistry', dueAt: d(27) },
    { externalId: 'chem-u1', kind: 'material', confidence: 0.9, title: 'Unit 1 Atomic structure', courseName: 'Chemistry', unitType: 'topics', quantity: 2, deadlineExternalId: 'chem-final' },
    { externalId: 'chem-u2', kind: 'material', confidence: 0.9, title: 'Unit 2 Bonding', courseName: 'Chemistry', unitType: 'topics', quantity: 2, deadlineExternalId: 'chem-final' },
    { externalId: 'chem-u3', kind: 'material', confidence: 0.9, title: 'Unit 3 Stoichiometry', courseName: 'Chemistry', unitType: 'problems', quantity: 15, deadlineExternalId: 'chem-final' },

    // Ambiguous: lands in the inbox for review.
    { externalId: 'chem-lab', kind: 'assignment', confidence: 0.55, title: 'Week 7 lab', courseName: 'Chemistry', dueAt: d(8) },
  ];
  return {
    items,
    busy: [{ date: d(1), startMin: 17 * 60 + 30, endMin: 19 * 60, title: 'Football practice' }],
    complete: true,
  };
};
