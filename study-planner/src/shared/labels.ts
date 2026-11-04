import type { Course, Deadline, Material, Session } from './types';

interface Lookup {
  courses: Course[];
  deadlines: Deadline[];
  materials: Material[];
}

export function materialName(db: Lookup, id: string): string {
  if (id.startsWith('practice:')) {
    const d = db.deadlines.find((x) => x.id === id.slice(9));
    return d ? `${d.kind === 'assignment' ? 'Extra time' : 'Practice'}: ${d.title}` : 'Practice';
  }
  if (id.startsWith('auto:')) {
    const d = db.deadlines.find((x) => x.id === id.slice(5));
    return d ? `Prepare: ${d.title}` : 'Study';
  }
  return db.materials.find((m) => m.id === id)?.title ?? 'Deleted material';
}

/** "Biology · Ch. 4 Cell structure" or "Biology · Review: Ch. 4, Ch. 5". */
export function sessionWhat(db: Lookup, s: Session): string {
  const names = (s.reviewMaterialIds ?? [s.materialId]).map((id) => materialName(db, id).replace(/^Prepare: /, ''));
  const list = `${names.slice(0, 3).join(', ')}${names.length > 3 ? ` +${names.length - 3}` : ''}`;
  if (s.kind === 'review') return `Review: ${list}`;
  if (s.materialId.startsWith('practice:')) return `${materialName(db, s.materialId)}${s.reviewMaterialIds?.length ? ` (${list})` : ''}`;
  return materialName(db, s.materialId);
}

export function sessionTitle(db: Lookup, s: Session): string {
  const course = db.courses.find((c) => c.id === s.courseId)?.name;
  const what = sessionWhat(db, s);
  return course ? `${course} · ${what}` : what;
}
