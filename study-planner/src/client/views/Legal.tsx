import { useEffect, useState } from 'react';
import { APP_NAME } from '../../shared/brand';

function useConfig() {
  const [cfg, setCfg] = useState<{ supportEmail: string | null; googleClientId: string | null } | null>(null);
  useEffect(() => {
    fetch('/api/config')
      .then((r) => r.json())
      .then(setCfg)
      .catch(() => {});
  }, []);
  return cfg;
}

function useContact() {
  return useConfig()?.supportEmail ?? null;
}

function Shell({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="legal">
      <a className="brand" href="/">
        <img src="/favicon.svg" alt="" width={28} height={28} />
        <span>{APP_NAME}</span>
      </a>
      <article className="card legal-body">
        <h1>{title}</h1>
        {children}
      </article>
    </div>
  );
}

export function Privacy() {
  const contact = useContact();
  return (
    <Shell title="Privacy Policy">
      <p className="muted small">Last updated: November 2026</p>
      <p>
        {APP_NAME} helps you plan study time. This policy explains what we collect, why, and the choices you have. We do not sell your data, show ads, or use
        tracking cookies.
      </p>
      <h2>What we collect</h2>
      <ul>
        <li>
          <strong>Account:</strong> your name, email address and a securely hashed password, or your Google account name and email if you sign in with Google.
        </li>
        <li>
          <strong>Study data you add:</strong> courses, exams, deadlines, study material, availability, study sessions, and the grades you choose to enter.
        </li>
        <li>
          <strong>Connected apps (read-only, only if you connect them):</strong> from Google Classroom, your courses, coursework, course materials and whether you
          turned work in; from Google Calendar, event times and titles from the calendars you pick; from calendar feed links you paste, the events in that feed.
        </li>
        <li>
          <strong>Documents you upload for analysis:</strong> processed to count pages and find chapters, then discarded. We keep only the resulting list of study
          units.
        </li>
        <li>
          <strong>Notifications:</strong> if you turn on reminders, your browser's push subscription address.
        </li>
        <li>
          <strong>Technical data:</strong> IP address and browser type in your login sessions and server logs, used for security.
        </li>
      </ul>
      <h2>How we use it, and why we're allowed to</h2>
      <p>
        Only to provide the service you signed up for: building and updating your study plan, keeping it in sync with apps you connect, sending the reminders
        you turn on, and keeping your account secure. The legal basis is our contract with you (providing the service), and our legitimate interest in keeping
        the service secure (security logs). We don't use your data for advertising or profiling, and we don't sell it.
      </p>
      <h2>Google user data</h2>
      <p>
        {APP_NAME}'s use and transfer of information received from Google APIs adheres to the{' '}
        <a href="https://developers.google.com/terms/api-services-user-data-policy" target="_blank" rel="noreferrer">
          Google API Services User Data Policy
        </a>
        , including the Limited Use requirements. We use Google data only to show and plan your coursework and calendar inside {APP_NAME}. We never use it for
        advertising, never sell it, never let humans read it except with your permission, for security, or as required by law, and never use it to train AI models.
      </p>
      <h2>Service providers</h2>
      <p>We use a small number of processors to run the service, under agreements that limit them to processing on our behalf:</p>
      <ul>
        <li>Hosting and database providers, which store your account and study data.</li>
        <li>An email provider, only if email reminders or account emails are turned on.</li>
        <li>
          Anthropic (Claude), only when an adult user asks the app to analyze a document or syllabus. We send the text of that document with contact details
          removed. Anthropic does not use API data to train its models. AI features are off for users under 18.
        </li>
      </ul>
      <h2>Cookies</h2>
      <p>We use one strictly necessary cookie to keep you signed in. No analytics or advertising cookies.</p>
      <h2>Retention and deletion</h2>
      <p>
        Your data stays until you delete it. You can download all of it as a file, or delete your account, at any time in Settings. Deleting your account removes
        your data from our database immediately and revokes our access to Google. Backups expire within 30 days.
      </p>
      <h2>Children and students</h2>
      <p>
        {APP_NAME} is not for children under 13. We ask for your birth month and year when you set up your account; if you're under 13, we delete the account
        right away. For users under 18 we use privacy-protective defaults: no ads, no profiling, and AI features switched off. School-managed Google accounts for
        students under 18 can only connect if the school's administrator allows the app.
      </p>
      <h2>Your rights</h2>
      <p>
        Depending on where you live (for example under the GDPR in the EU/UK), you can access, correct, export or delete your data and object to processing. Use
        the tools in Settings or contact us; we answer within one month. You can also complain to your local data protection authority.
      </p>
      <h2>Contact</h2>
      <p>{contact ? <a href={`mailto:${contact}`}>{contact}</a> : 'Contact details are provided by the operator of this site.'}</p>
    </Shell>
  );
}

export function Terms() {
  const contact = useContact();
  return (
    <Shell title="Terms of Use">
      <p className="muted small">Last updated: November 2026</p>
      <p>By creating an account you agree to these terms.</p>
      <h2>The service</h2>
      <p>
        {APP_NAME} suggests study plans based on the material and dates you provide or connect. Plans and time estimates are suggestions; you remain responsible
        for checking real deadlines and requirements with your school or teachers.
      </p>
      <h2>Your account</h2>
      <ul>
        <li>You must be at least 13, or have a parent's or guardian's permission.</li>
        <li>Keep your password safe. You're responsible for activity in your account.</li>
        <li>Only upload documents you have the right to use.</li>
      </ul>
      <h2>Acceptable use</h2>
      <p>Don't misuse the service: no attempts to break security, overload it, access other people's data, or use it for anything illegal.</p>
      <h2>AI features</h2>
      <p>Document and syllabus analysis is automated and can be wrong. Nothing it suggests is added to your plan until you review and accept it.</p>
      <h2>Availability and changes</h2>
      <p>We work to keep the service running but can't guarantee it will always be available. We may change features or these terms and will tell you about important changes.</p>
      <h2>Ending your account</h2>
      <p>You can delete your account at any time in Settings. We may suspend accounts that break these terms.</p>
      <h2>Liability</h2>
      <p>The service is provided "as is". To the extent the law allows, we aren't liable for indirect losses, such as missed deadlines or grades.</p>
      <h2>Contact</h2>
      <p>{contact ? <a href={`mailto:${contact}`}>{contact}</a> : 'Contact details are provided by the operator of this site.'}</p>
    </Shell>
  );
}

const SCOPES = [
  ['classroom.courses.readonly', 'See the names of your classes'],
  ['classroom.coursework.me.readonly', 'See your own assignments, tests and whether you turned them in'],
  ['classroom.courseworkmaterials.readonly', 'See course materials your teachers post'],
  ['classroom.topics.readonly', 'See how coursework is grouped into topics'],
  ['calendar.calendarlist.readonly', 'See the list of your calendars (you choose which to read)'],
  ['calendar.events.readonly', 'See event times in the calendars you choose, to avoid busy times'],
];

/** For school IT: what the app reads and how to allow it for students under 18. */
export function Schools() {
  const cfg = useConfig();
  const site = location.origin;
  const email = `Hello,

I'd like to use ${APP_NAME} (${site}) to plan my study time from Google Classroom. Because I'm under 18, it needs to be allowed for our school's Google Workspace.

It only reads (never changes) my classes, coursework, course materials and calendar busy times. In the Admin console: Security → Access and data control → API controls → Manage third-party app access → Add app → OAuth App Name or Client ID, then search for:
${cfg?.googleClientId ?? '(client ID shown on ' + site + '/schools)'}
and set it to "Trusted" (or "Limited" with the scopes listed on ${site}/schools).

Details and privacy policy: ${site}/schools and ${site}/privacy

Thank you!`;
  return (
    <Shell title="For school IT administrators">
      <p>
        Google Workspace for Education blocks students under 18 from apps the school hasn't configured. If your students want to use {APP_NAME} with Google
        Classroom, an administrator can allow it in a few clicks. {APP_NAME} only <strong>reads</strong> data; it never posts, edits or deletes anything.
      </p>
      <h2>The app</h2>
      <ul>
        <li>
          Name: <strong>{APP_NAME}</strong> · Website: <a href={site}>{site}</a> · <a href="/privacy">Privacy policy</a>
        </li>
        <li>
          OAuth client ID: <code>{cfg?.googleClientId ?? 'not configured on this site yet'}</code>
        </li>
      </ul>
      <h2>Permissions it asks for (all read-only)</h2>
      <ul>
        {SCOPES.map(([s, what]) => (
          <li key={s}>
            <code>https://www.googleapis.com/auth/{s}</code>: {what}
          </li>
        ))}
      </ul>
      <h2>How to allow it</h2>
      <ol>
        <li>Open the Google Admin console (admin.google.com) as a super administrator.</li>
        <li>Go to Security → Access and data control → API controls → Manage third-party app access.</li>
        <li>Click Add app → OAuth App Name or Client ID, search for the client ID above, and select it.</li>
        <li>Choose the organizational units for your students and set access to Trusted (or Limited to the scopes above).</li>
      </ol>
      <h2>Email students can forward</h2>
      <textarea readOnly rows={14} value={email} style={{ width: '100%' }} onFocus={(e) => e.currentTarget.select()} aria-label="Email to send to school IT" />
      <p className="small muted">Students can always use {APP_NAME} without Google too, with their school's calendar feed or by adding exams themselves.</p>
    </Shell>
  );
}
