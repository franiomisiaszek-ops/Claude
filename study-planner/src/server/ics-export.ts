import { addDays, hhmmToMin, minToHhmm } from '../shared/dates';
import type { UserState } from '../shared/types';
import { sessionTitle } from '../shared/labels';
import { todayFor } from './plan-service';

const esc = (s: string) => s.replace(/\\/g, '\\\\').replace(/;/g, '\\;').replace(/,/g, '\\,').replace(/\r?\n/g, '\\n');

/** Folds lines to 75 octets as RFC 5545 requires. */
function fold(line: string): string {
  const bytes = Buffer.from(line, 'utf8');
  if (bytes.length <= 75) return line;
  const parts: string[] = [];
  let cur = '';
  for (const ch of line) {
    if (Buffer.byteLength(cur + ch, 'utf8') > (parts.length ? 74 : 75)) {
      parts.push(cur);
      cur = '';
    }
    cur += ch;
  }
  parts.push(cur);
  return parts.join('\r\n ');
}

const ymd = (d: string) => d.replace(/-/g, '');

/**
 * The plan as an .ics file. Sessions get times inside the study window when one is set,
 * otherwise they are all-day events. Each has an alarm so the calendar app reminds you.
 */
export function exportIcs(db: UserState): string {
  const today = todayFor(db);
  const stamp = new Date().toISOString().replace(/[-:]/g, '').replace(/\.\d+/, '');
  const lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Cramless//Study Plan//EN', 'CALSCALE:GREGORIAN', 'X-WR-CALNAME:Study plan'];
  const win = db.availability.studyWindow;
  const cursor = new Map<string, number>();
  for (const s of db.sessions.filter((x) => x.date >= today && x.status === 'planned')) {
    lines.push('BEGIN:VEVENT', `UID:${s.id}@cramless`, `DTSTAMP:${stamp}`);
    if (win) {
      const start = cursor.get(s.date) ?? hhmmToMin(win.start);
      const end = start + s.minutes;
      cursor.set(s.date, end + 10);
      lines.push(`DTSTART:${ymd(s.date)}T${minToHhmm(Math.min(start, 1439)).replace(':', '')}00`, `DTEND:${ymd(s.date)}T${minToHhmm(Math.min(end, 1439)).replace(':', '')}00`);
    } else {
      lines.push(`DTSTART;VALUE=DATE:${ymd(s.date)}`, `DTEND;VALUE=DATE:${ymd(addDays(s.date, 1))}`, 'TRANSP:TRANSPARENT');
    }
    lines.push(fold(`SUMMARY:${esc(`📚 ${sessionTitle(db, s)} (${s.minutes} min)`)}`));
    lines.push('BEGIN:VALARM', 'ACTION:DISPLAY', fold(`DESCRIPTION:${esc(sessionTitle(db, s))}`), `TRIGGER:${win ? '-PT0M' : 'PT8H'}`, 'END:VALARM', 'END:VEVENT');
  }
  for (const d of db.deadlines.filter((x) => x.status === 'active' && x.dueAt >= today)) {
    const course = db.courses.find((c) => c.id === d.courseId)?.name ?? '';
    lines.push(
      'BEGIN:VEVENT',
      `UID:${d.id}@cramless`,
      `DTSTAMP:${stamp}`,
      `DTSTART;VALUE=DATE:${ymd(d.dueAt)}`,
      `DTEND;VALUE=DATE:${ymd(addDays(d.dueAt, 1))}`,
      fold(`SUMMARY:${esc(`🎯 ${d.title} (${d.kind}${course ? `, ${course}` : ''})`)}`),
      'END:VEVENT',
    );
  }
  lines.push('END:VCALENDAR');
  return lines.join('\r\n') + '\r\n';
}
