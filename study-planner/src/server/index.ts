/** Server entry: connect the database, run migrations, start the API and background jobs. */
import { APP_NAME } from '../shared/brand';
import { createApp } from './app';
import { createAuth } from './auth';
import { connect } from './db';
import { env, features } from './env';
import { startJobs } from './jobs';
import { ensureProfile } from './repo';
import { assertProductionConfig, ensurePushKeys } from './setup';

assertProductionConfig();
const { db, kind } = await connect({ url: env.databaseUrl, dataDir: env.dataDir });
await ensurePushKeys(db);
const auth = createAuth(db, { onUserCreated: (id) => ensureProfile(db, id) });
const app = createApp({ db, auth });
if (env.inProcessJobs) startJobs(db);

app.listen(env.port, () => {
  const on = Object.entries(features)
    .filter(([, f]) => f())
    .map(([k]) => k);
  console.log(`${APP_NAME} running at ${env.publicUrl} (database: ${kind}; optional features on: ${on.join(', ') || 'none'})`);
});
