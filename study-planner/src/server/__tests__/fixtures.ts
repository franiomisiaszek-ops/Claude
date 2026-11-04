import type { UserState } from '../../shared/types';
import { defaultAvailability, defaultSettings } from '../repo';

export const emptyState = (): UserState => ({
  settings: defaultSettings('UTC'),
  availability: defaultAvailability(),
  blocks: [],
  courses: [],
  deadlines: [],
  materials: [],
  sessions: [],
  busy: [],
  sources: [],
  inbox: [],
  snapshots: [],
  digestLog: [],
  results: [],
  planMeta: { health: { status: 'ok', deadlines: [] } },
});
