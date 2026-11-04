import type { Reason } from './types';

const days = (n: number) => (n === 1 ? '1 day' : `${n} days`);

/** Turns a planner reason code into a human sentence for "Why today?". */
export function explain(r: Reason): string {
  switch (r.code) {
    case 'deadline':
      return `${r.title} (${r.kind}) is in ${days(r.daysLeft ?? 0)}`;
    case 'progress':
      return `${r.doneMin ?? 0} of ${r.totalMin ?? 0} min done so far`;
    case 'catchup':
      return 'Catching up on a session you missed or skipped';
    case 'bufferShrunk':
      return 'The usual buffer before the deadline was shortened to fit everything in';
    case 'selfPaced':
      return 'No due date, so it fills free time over the next few weeks';
    case 'review':
      return `Spaced review, ${days(r.offset ?? 1)} after you learned it`;
    case 'bufferReview':
      return `Review day before ${r.title}: no new material, just consolidation`;
    case 'pinned':
      return 'You placed this session yourself, so re-planning leaves it alone';
    case 'rough':
      return 'Uses the time you chose for this, because its material isn\'t listed yet';
    case 'learned':
      return `Your past grades in this course suggest ×${(r.factor ?? 1).toFixed(2)} the material time to reach your target`;
    case 'practice':
      return 'Extra practice beyond one pass through the material: from "more time" you asked for or what your grades showed you need';
    case 'weak':
      return 'You marked this as a weak topic after an exam, so it gets extra review';
    default:
      return '';
  }
}
